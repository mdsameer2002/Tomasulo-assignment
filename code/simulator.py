"""Tomasulo with in-order multiple issue, one CDB and precise retirement.

The supplied Qing (2017) modules are preserved in ../starter. This engine uses
explicit mutable records to fix their aliasing, timing and recovery problems.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import re


@dataclass
class Unit:
    stations: int
    latency: int
    count: int = 1


@dataclass
class Config:
    issue_width: int = 1
    rob_entries: int = 64
    integer: Unit = field(default_factory=lambda: Unit(4, 1))
    adder: Unit = field(default_factory=lambda: Unit(3, 4))
    multiplier: Unit = field(default_factory=lambda: Unit(2, 15))
    memory: Unit = field(default_factory=lambda: Unit(5, 1))
    memory_latency: int = 5

    def validate(self):
        if self.issue_width not in range(1, 5):
            raise ValueError('Issue width must be an integer from 1 to 4')
        if self.rob_entries < 1 or self.memory_latency < 1:
            raise ValueError('ROB size and memory latency must be positive')
        for unit in (self.integer, self.adder, self.multiplier, self.memory):
            if min(unit.stations, unit.latency, unit.count) < 1:
                raise ValueError('Station counts, FU counts, and latencies must be positive')


@dataclass(frozen=True)
class Instruction:
    text: str
    op: str
    destination: str | None
    sources: tuple[str | int, ...]
    immediate: int = 0

    @property
    def kind(self):
        if self.op in ('ld', 'sd'):
            return 'memory'
        if self.op == 'mult.d':
            return 'multiplier'
        return 'adder' if self.op in ('add.d', 'sub.d') else 'integer'


def register(token, bank=None):
    token = token.upper()
    if not re.fullmatch(r'[RF](?:[0-9]|[12][0-9]|3[01])', token):
        raise ValueError(f'Invalid register: {token}')
    if bank and token[0] != bank:
        raise ValueError(f'Expected a {bank} register, got {token}')
    return token


def parse_instruction(text):
    parts = text.replace(',', ' ').split()
    op = parts[0].lower()
    if op in ('ld', 'sd') and len(parts) == 3:
        match = re.fullmatch(r'(-?\d+)\(([rR]\d+)\)', parts[2])
        if not match:
            raise ValueError(f'Invalid memory operand: {text}')
        fp, base = register(parts[1], 'F'), register(match[2], 'R')
        return Instruction(text, op, fp if op == 'ld' else None,
                           (base,) if op == 'ld' else (base, fp), int(match[1]))
    if len(parts) != 4:
        raise ValueError(f'Invalid instruction: {text}')
    if op in ('beq', 'bne'):
        return Instruction(text, op, None,
                           (register(parts[1], 'R'), register(parts[2], 'R')), int(parts[3]))
    if op not in ('add', 'addi', 'sub', 'add.d', 'sub.d', 'mult.d'):
        raise ValueError(f'Unsupported opcode: {op}')
    bank = 'F' if op.endswith('.d') else 'R'
    second = int(parts[3]) if op == 'addi' else register(parts[3], bank)
    return Instruction(text, op, register(parts[1], bank), (register(parts[2], bank), second))


def check_address(address):
    # The assignment example includes byte address 18; alignment is not required.
    if not isinstance(address, int) or not 0 <= address <= 252:
        raise ValueError(f'Word at byte address {address} lies outside 256-byte memory')


def read_program(path):
    """Read hardware, initial values and assembly in the assignment's format."""
    config, registers, memory, instructions = Config(), {}, {}, []
    for number, raw in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        try:
            matched = False
            for label, key in [('Integer adder', 'integer'), ('FP adder', 'adder'),
                               ('FP multiplier', 'multiplier'), ('Load/store unit', 'memory')]:
                if not line.lower().startswith(label.lower()):
                    continue
                values = [int(x) for x in line[len(label):].split()]
                if len(values) != (4 if key == 'memory' else 3):
                    raise ValueError(f'Wrong number of hardware parameters for {label}')
                if key == 'memory':
                    config.memory = Unit(values[0], values[1], values[3])
                    config.memory_latency = values[2]
                else:
                    setattr(config, key, Unit(*values))
                matched = True
                break
            if matched:
                continue
            setting = re.fullmatch(r'(ROB entries|Issue width)\s*=\s*(\d+)', line, re.I)
            if setting:
                setattr(config, 'rob_entries' if setting[1].lower() == 'rob entries' else 'issue_width', int(setting[2]))
            elif '=' in line:
                for assignment in line.split(','):
                    name, value = (part.strip() for part in assignment.split('='))
                    cell = re.fullmatch(r'Mem\[(\d+)\]', name, re.I)
                    if cell:
                        address = int(cell[1])
                        check_address(address)
                        memory[address] = float(value)
                    else:
                        reg = register(name)
                        registers[reg] = int(value) if reg.startswith('R') else float(value)
            else:
                instructions.append(parse_instruction(line))
        except (ValueError, IndexError) as error:
            raise ValueError(f'{path}:{number}: {error}') from error
    config.validate()
    registers['R0'] = 0
    return config, registers, memory, instructions


@dataclass
class Operand:
    value: float | int = 0
    tag: int | None = None


@dataclass
class Entry:
    tag: int
    pc: int
    instruction: Instruction
    operands: list[Operand]
    issue: int
    ex: list[int] = field(default_factory=list)
    mem: list[int] = field(default_factory=list)
    wb: int | None = None
    commit: int | None = None
    value: float | int | None = None
    address: int | None = None
    predicted_next: int | None = None
    resolved: int | None = None
    squashed: bool = False


class Machine:
    """FP units are pipelined; integer units and AGUs are unpipelined.

    Memory FU count controls AGUs; all share one nonpipelined memory port.
    Register values and memory words use Python numbers (no rounding model).
    """
    def __init__(self, config, registers, memory, instructions):
        config.validate()
        self.config, self.instructions = config, instructions
        self.registers = {f'{bank}{i}': 0 for bank in 'RF' for i in range(32)}
        self.registers.update(registers)
        self.registers['R0'] = 0
        self.memory, self.rat = dict(memory), {}
        self.rob, self.entries, self.status = deque(), {}, []
        self.rs = {kind: [] for kind in ('integer', 'adder', 'multiplier')}
        self.lsq, self.executing, self.results = [], [], []
        self.memory_job = None  # (entry, last MEM cycle, value, is_store)
        self.btb = [None] * 8  # (word PC tag, taken bit, target word PC)
        self.pc = self.cycle = self.next_tag = self.fetch_resume = 0
        self.mispredictions = self.memory_replays = 0
        self.committed = []
        self.port_used = False
        self.retired_this_cycle = False

    def operand(self, source):
        if isinstance(source, int):
            return Operand(source)
        tag = self.rat.get(source)
        if tag is None:
            return Operand(self.registers[source])
        producer = self.entries[tag]
        return Operand(producer.value) if producer.wb is not None else Operand(tag=tag)

    def squash_after(self, tag, restart):
        for entry in self.rob:
            if entry.tag > tag:
                entry.squashed = True
        self.rob = deque(entry for entry in self.rob if entry.tag <= tag)
        self.rat.clear()
        for entry in self.rob:
            destination = entry.instruction.destination
            if destination and destination != 'R0':
                self.rat[destination] = entry.tag
        for kind in self.rs:
            self.rs[kind] = [entry for entry in self.rs[kind] if entry.tag <= tag]
        self.lsq = [entry for entry in self.lsq if entry.tag <= tag]
        self.executing = [job for job in self.executing if job[0].tag <= tag]
        self.results = [job for job in self.results if job[1] <= tag]
        if self.memory_job and self.memory_job[0].tag > tag:
            self.memory_job = None
        self.pc, self.fetch_resume = restart, self.cycle + 2

    def issue(self):
        if self.cycle < self.fetch_resume:
            return
        for _ in range(self.config.issue_width):
            if self.pc == len(self.instructions):
                break
            if not 0 <= self.pc < len(self.instructions):
                raise ValueError(f'Branch target word PC {self.pc} is outside the program')
            ins = self.instructions[self.pc]
            unit = getattr(self.config, ins.kind)
            station = self.lsq if ins.kind == 'memory' else self.rs[ins.kind]
            if len(self.rob) == self.config.rob_entries or len(station) == unit.stations:
                break  # in-order bundle stops at first structural hazard
            entry = Entry(self.next_tag, self.pc, ins,
                          [self.operand(source) for source in ins.sources], self.cycle)
            self.next_tag += 1
            self.entries[entry.tag] = entry
            self.status.append(entry)
            self.rob.append(entry)
            station.append(entry)
            if ins.destination and ins.destination != 'R0':
                self.rat[ins.destination] = entry.tag
            next_pc = self.pc + 1
            if ins.op in ('beq', 'bne'):
                prediction = self.btb[self.pc & 7]
                if prediction and prediction[0] == self.pc and prediction[1]:
                    next_pc = prediction[2]
                entry.predicted_next = next_pc
            self.pc = next_pc

    def execute(self):
        for kind in ('integer', 'adder', 'multiplier', 'memory'):
            unit = getattr(self.config, kind)
            station = self.lsq if kind == 'memory' else self.rs[kind]
            busy = {job[2] for job in self.executing if job[0].instruction.kind == kind}
            units = range(unit.count) if kind in ('adder', 'multiplier') else [i for i in range(unit.count) if i not in busy]
            ready = [entry for entry in station if not entry.ex and entry.issue < self.cycle
                     and all(op.tag is None for op in (entry.operands[:1] if kind == 'memory' else entry.operands))]
            for index, entry in zip(units, ready):
                entry.ex = [self.cycle, self.cycle + unit.latency - 1]
                self.executing.append((entry, entry.ex[1], index))
                if kind != 'memory':
                    station.remove(entry)
        finished = sorted([job for job in self.executing if job[1] == self.cycle], key=lambda job: job[0].tag)
        self.executing = [job for job in self.executing if job[1] != self.cycle]
        for entry, _, _ in finished:
            if entry.squashed:
                continue
            ins, values = entry.instruction, [op.value for op in entry.operands]
            if ins.kind == 'memory':
                entry.address = values[0] + ins.immediate
                if ins.op == 'sd':
                    conflicts = [load for load in self.rob if load.tag > entry.tag
                                 and load.instruction.op == 'ld' and load.mem and load.address == entry.address]
                    if conflicts:
                        load = conflicts[0]
                        self.memory_replays += 1
                        self.squash_after(load.tag - 1, load.pc)
            elif ins.op in ('beq', 'bne'):
                taken = (values[0] == values[1]) == (ins.op == 'beq')
                target = entry.pc + 1 + ins.immediate
                actual = target if taken else entry.pc + 1
                entry.resolved = self.cycle
                self.btb[entry.pc & 7] = (entry.pc, taken, target)
                if actual != entry.predicted_next:
                    self.mispredictions += 1
                    self.squash_after(entry.tag, actual)
            else:
                if ins.op in ('add', 'addi', 'add.d'):
                    value = values[0] + values[1]
                elif ins.op in ('sub', 'sub.d'):
                    value = values[0] - values[1]
                else:
                    value = values[0] * values[1]
                self.results.append((self.cycle + 1, entry.tag, value))

    def memory_stage(self):
        if self.memory_job:
            entry, end, value, store = self.memory_job
            self.port_used = True
            if self.cycle == end:
                if store:
                    self.memory[entry.address] = value
                    self.retire(entry)
                else:
                    self.results.append((self.cycle + 1, entry.tag, value))
                    self.lsq.remove(entry)
                self.memory_job = None
            return
        if self.port_used:
            return
        for entry in self.lsq:
            if entry.instruction.op != 'ld' or not entry.ex or entry.ex[1] >= self.cycle:
                continue
            older = [store for store in self.lsq if store.tag < entry.tag and store.instruction.op == 'sd']
            match = next((store for store in reversed(older) if store.address == entry.address), None)
            if match and match.operands[1].tag is not None:
                continue
            value = match.operands[1].value if match else self.memory.get(entry.address, 0)
            duration = 1 if match else self.config.memory_latency
            entry.mem = [self.cycle, self.cycle + duration - 1]
            self.memory_job = (entry, entry.mem[1], value, False)
            self.port_used = True
            if duration == 1:
                self.results.append((self.cycle + 1, entry.tag, value))
                self.lsq.remove(entry)
                self.memory_job = None
            return

    def writeback(self):
        ready = [result for result in self.results if result[0] <= self.cycle]
        if not ready:
            return
        result = min(ready)
        self.results.remove(result)
        _, tag, value = result
        entry = self.entries[tag]
        entry.value, entry.wb = value, self.cycle
        for waiter in self.rob:
            for operand in waiter.operands:
                if operand.tag == tag:
                    operand.value, operand.tag = value, None

    def retire(self, entry):
        assert self.rob[0] is entry and not self.retired_this_cycle
        self.retired_this_cycle = True
        self.rob.popleft()
        entry.commit = self.cycle
        self.committed.append(entry)
        destination = entry.instruction.destination
        if destination and destination != 'R0':
            self.registers[destination] = entry.value
            if self.rat.get(destination) == entry.tag:
                del self.rat[destination]
        if entry in self.lsq:
            self.lsq.remove(entry)

    def commit_stage(self):
        if not self.rob:
            return
        entry = self.rob[0]
        if entry.instruction.op == 'sd':
            if (entry.ex and entry.ex[1] < self.cycle and entry.operands[1].tag is None
                    and not self.memory_job):
                check_address(entry.address)
                end = self.cycle + self.config.memory_latency - 1
                entry.mem = [self.cycle, end]
                self.memory_job = (entry, end, entry.operands[1].value, True)
                if end == self.cycle:
                    self.port_used = True
                    self.memory[entry.address] = entry.operands[1].value
                    self.retire(entry)
                    self.memory_job = None
        elif (entry.resolved is not None and entry.resolved < self.cycle) or (entry.wb is not None and entry.wb < self.cycle):
            if entry.instruction.op == 'ld':
                check_address(entry.address)  # faults are precise, after speculation
            self.retire(entry)

    def run(self, max_cycles=100000):
        while self.rob or self.pc != len(self.instructions):
            self.cycle += 1
            if self.cycle > max_cycles:
                raise RuntimeError(f'Exceeded {max_cycles} cycles; possible infinite loop')
            self.port_used = self.retired_this_cycle = False
            # Issue uses cycle-start space. Commit precedes WB; MEM precedes EX;
            # EX precedes WB. All operands cross a cycle boundary between stages.
            self.issue()
            self.commit_stage()
            self.memory_stage()
            self.execute()
            self.writeback()
        return self

    def as_dict(self):
        return {
            'issue_width': self.config.issue_width, 'cycles': self.cycle,
            'committed': len(self.committed), 'mispredictions': self.mispredictions,
            'memory_replays': self.memory_replays, 'registers': self.registers,
            'memory': {str(a): v for a, v in sorted(self.memory.items()) if v != 0},
            'instructions': [
                {'tag': e.tag, 'pc': e.pc * 4, 'instruction': e.instruction.text,
                 'issue': e.issue, 'ex': e.ex, 'mem': e.mem,
                 'wb': e.wb, 'commit': e.commit, 'squashed': e.squashed}
                for e in self.status],
        }

    def format_output(self):
        lines = [f'Issue width = {self.config.issue_width}',
                 f'{"Instruction":<34} {"ISSUE":>6} {"EX":>11} {"MEM":>11} {"WB":>6} {"COMMIT":>8}']
        for entry in self.committed:
            interval = lambda values: '-'.join(map(str, values)) if values else '-'
            lines.append(f'{entry.instruction.text:<34} {entry.issue:>6} {interval(entry.ex):>11} '
                         f'{interval(entry.mem):>11} {str(entry.wb or "-"):>6} {entry.commit:>8}')
        lines.append('\nREGISTER VALUES')
        for bank in 'RF':
            for start in range(0, 32, 8):
                lines.append('  '.join(f'{bank}{i}={self.registers[f"{bank}{i}"]:g}' for i in range(start, start + 8)))
        lines.append('\nNON-ZERO MEMORY VALUES\nAddress  Value')
        lines.extend(f'{a:>7}  {v:g}' for a, v in sorted(self.memory.items()) if v != 0)
        lines.append(f'\nCycles: {self.cycle}; committed: {len(self.committed)}; '
                     f'branch mispredictions: {self.mispredictions}; memory replays: {self.memory_replays}')
        return '\n'.join(lines)
