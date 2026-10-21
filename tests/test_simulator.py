"""Architectural oracle plus checks of issue, timing, CDB and recovery rules."""
import copy
from collections import Counter
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from simulator import Config, Machine, Unit, parse_instruction, read_program


def reference(instructions, registers, memory):
    """Independent sequential ISA interpreter, without scheduling or renaming."""
    regs = {f'{bank}{i}': 0 for bank in 'RF' for i in range(32)}
    regs.update(registers)
    regs['R0'] = 0
    mem, pc, count = dict(memory), 0, 0
    while pc < len(instructions):
        count += 1
        if count > 10000:
            raise RuntimeError('Reference program does not terminate')
        ins = instructions[pc]
        values = [value if isinstance(value, int) else regs[value] for value in ins.sources]
        next_pc = pc + 1
        if ins.op in ('add', 'addi', 'add.d'):
            result = values[0] + values[1]
        elif ins.op in ('sub', 'sub.d'):
            result = values[0] - values[1]
        elif ins.op == 'mult.d':
            result = values[0] * values[1]
        elif ins.op == 'ld':
            result = mem.get(values[0] + ins.immediate, 0)
        elif ins.op == 'sd':
            mem[values[0] + ins.immediate] = values[1]
        elif (values[0] == values[1]) == (ins.op == 'beq'):
            next_pc += ins.immediate
        if ins.destination and ins.destination != 'R0':
            regs[ins.destination] = result
        pc = next_pc
    return regs, {k: v for k, v in mem.items() if v != 0}, count


class SimulatorTests(unittest.TestCase):
    def verify(self, text, registers=None, memory=None, config=None):
        instructions = [parse_instruction(line) for line in text.strip().splitlines() if line.strip()]
        config, registers, memory = config or Config(), registers or {}, memory or {}
        expected_regs, expected_mem, expected_count = reference(instructions, registers, memory)
        machines = []
        for width in range(1, 5):
            with self.subTest(width=width):
                settings = copy.deepcopy(config)
                settings.issue_width = width
                machine = Machine(settings, registers, memory, instructions).run(5000)
                self.assertEqual(machine.registers, expected_regs)
                self.assertEqual({k: v for k, v in machine.memory.items() if v != 0}, expected_mem)
                self.assertEqual(len(machine.committed), expected_count)
                self.assertEqual(len(machine.rob), 0)
                self.assertFalse(machine.rat)
                self.assertFalse(machine.lsq)
                self.assertLessEqual(max(Counter(e.issue for e in machine.status).values(), default=0), width)
                self.assertLessEqual(max(Counter(e.wb for e in machine.status if e.wb).values(), default=0), 1)
                self.assertEqual(len({e.commit for e in machine.committed}), expected_count)
                tags = [e.tag for e in machine.committed]
                self.assertEqual(tags, sorted(tags))
                for e in machine.committed:
                    self.assertGreater(e.ex[0], e.issue)
                    self.assertEqual(e.ex[1] - e.ex[0] + 1, getattr(settings, e.instruction.kind).latency)
                    self.assertGreater(e.commit, e.ex[1])
                    if e.wb:
                        self.assertGreater(e.wb, (e.mem or e.ex)[1])
                        self.assertGreater(e.commit, e.wb)
                machines.append(machine)
        return machines

    def test_same_bundle_raw_waw_war(self):
        self.verify('''Mult.d F1 F2 F3
Add.d F4 F1 F2
Add.d F1 F2 F2
Add.d F5 F1 F1
Addi R1 R0 5
Addi R1 R1 2
Add R2 R1 R1
Addi R0 R2 99
Sub R3 R2 R1''', {'F2': 2.0, 'F3': 3.0})

    def test_forward_nearest_matching_store(self):
        machines = self.verify('''Mult.d F10 F1 F1
Sd F1 0(R0)
Sd F2 4(R0)
Ld F3 0(R0)
Ld F4 4(R0)
Add.d F5 F3 F4''', {'F1': 7.0, 'F2': 9.0})
        self.assertTrue(any(e.instruction.op == 'ld' and e.mem[0] == e.mem[1]
                            for m in machines for e in m.committed))

    def test_late_store_address_replays_load(self):
        config = Config(integer=Unit(4, 7), memory_latency=2)
        machines = self.verify('''Addi R10 R0 8
Sd F1 0(R10)
Ld F2 8(R0)
Add.d F3 F2 F2''', {'F1': 9.0}, {8: 1.0}, config)
        self.assertTrue(all(m.memory_replays >= 1 for m in machines))

    def test_taken_branch_squashes_store_and_register(self):
        machines = self.verify('''Beq R1 R1 2
Sd F1 0(R0)
Addi R2 R0 999
Addi R2 R0 7''', {'R1': 1, 'F1': 6.0}, {0: 3.0}, Config(integer=Unit(4, 4)))
        for machine in machines:
            self.assertEqual(machine.mispredictions, 1)
            branch = machine.committed[0]
            target = machine.committed[1]
            self.assertEqual(target.issue, branch.ex[1] + 2)

    def test_wrong_path_invalid_load_is_squashed(self):
        self.verify('''Beq R0 R0 1
Ld F1 1000(R0)
Addi R2 R0 7''', config=Config(integer=Unit(4, 7), memory_latency=1))

    def test_branch_restores_older_rename_and_cancels_inflight_work(self):
        self.verify('''Mult.d F1 F2 F3
Addi R1 R0 1
Beq R1 R1 3
Add.d F1 F2 F2
Ld F6 0(R0)
Sd F6 4(R0)
Add.d F4 F1 F1''', {'F2': 2.0, 'F3': 3.0}, {0: 9.0},
                    Config(integer=Unit(4, 3), multiplier=Unit(4, 20)))

    def test_branches_loop_and_btb_conflicts(self):
        self.verify('''Addi R1 R1 -1
Bne R1 R0 -2
Beq R1 R0 1
Addi R2 R0 999
Addi R2 R0 4
Beq R1 R2 1
Add R3 R2 R2
Addi R4 R0 1
Addi R4 R4 -1
Bne R4 R0 -2''', {'R1': 5})

    def test_head_of_line_stall(self):
        machines = self.verify('''Add R1 R0 R0
Add R2 R0 R0
Add.d F1 F0 F0''', config=Config(integer=Unit(1, 4)))
        for m in machines:
            self.assertGreater(m.committed[1].issue, m.committed[0].issue)
            self.assertGreaterEqual(m.committed[2].issue, m.committed[1].issue)

    def test_rob_and_lsq_backpressure(self):
        self.verify('''Ld F1 0(R0)
Sd F1 4(R0)
Ld F2 4(R0)
Add.d F3 F1 F2''', memory={0: 2.0}, config=Config(rob_entries=1, memory=Unit(1, 3), memory_latency=2))

    def test_one_port_with_one_cycle_memory(self):
        machines = self.verify('''Sd F1 0(R0)
Ld F2 4(R0)
Ld F3 0(R0)''', {'F1': 3.0}, {4: 9.0}, Config(memory=Unit(4, 1, 2), memory_latency=1))
        for m in machines:
            accesses = [cycle for e in m.committed for cycle in range(e.mem[0], e.mem[1] + 1)]
            self.assertEqual(len(accesses), len(set(accesses)))

    def test_integer_unpipelined_and_fp_pipelined(self):
        config = Config(integer=Unit(6, 3), adder=Unit(6, 4))
        machines = self.verify('''Add R1 R0 R0
Add R2 R0 R0
Add.d F1 F0 F0
Add.d F2 F0 F0
Add.d F3 F0 F0''', config=config)
        for m in machines:
            self.assertGreater(m.committed[1].ex[0], m.committed[0].ex[1])
        m = machines[-1]
        self.assertLess(m.committed[3].ex[0], m.committed[2].ex[1])

    def test_multiple_function_units(self):
        config = Config(integer=Unit(8, 3, 2), adder=Unit(8, 4, 2), memory=Unit(8, 2, 2))
        machines = self.verify('''Add R1 R0 R0
Add R2 R0 R0
Add.d F1 F0 F0
Add.d F2 F0 F0
Ld F3 0(R0)
Ld F4 4(R0)''', config=config)
        self.assertEqual(machines[-1].committed[0].ex, machines[-1].committed[1].ex)

    def test_supplied_input(self):
        config, regs, memory, instructions = read_program(ROOT / 'code' / 'test_case.txt')
        machines = self.verify('\n'.join(i.text for i in instructions), regs, memory, config)
        for m in machines:
            self.assertEqual(m.registers['R1'], 0)
            self.assertEqual(m.registers['F20'], 2)
            self.assertEqual(m.memory[32], 7)

    def test_generated_programs_against_sequential_oracle(self):
        rng = random.Random(2048)
        for trial in range(25):
            lines = []
            for _ in range(35):
                op = rng.choice(['addi', 'add', 'sub', 'add.d', 'sub.d', 'mult.d', 'ld', 'sd'])
                if op == 'addi':
                    lines.append(f'Addi R{rng.randrange(6)} R{rng.randrange(6)} {rng.randrange(-3, 4)}')
                elif op in ('ld', 'sd'):
                    lines.append(f'{op} F{rng.randrange(6)} {4 * rng.randrange(6)}(R0)')
                else:
                    bank = 'F' if op.endswith('.d') else 'R'
                    lines.append(f'{op} {bank}{rng.randrange(6)} {bank}{rng.randrange(6)} {bank}{rng.randrange(6)}')
            with self.subTest(trial=trial):
                self.verify('\n'.join(lines), {'F1': 1.0, 'F2': 2.0}, {0: 3.0, 4: 4.0},
                            Config(rob_entries=7, integer=Unit(2, 3), memory=Unit(3, 2), memory_latency=3))

    def test_validation_and_empty_program(self):
        for width in (0, 5):
            with self.assertRaises(ValueError):
                Machine(Config(issue_width=width), {}, {}, [])
        self.assertEqual(Machine(Config(), {}, {}, []).run().cycle, 0)
        with self.assertRaises(ValueError):
            parse_instruction('Add R32 R0 R1')
        with self.assertRaises(ValueError):
            Machine(Config(), {}, {}, [parse_instruction('Ld F1 256(R0)')]).run()

    def test_configuration_and_case_insensitive_input(self):
        text = '''# hardware and assembly may use mixed case
Integer adder 2 2 2
FP adder 3 4 1
FP multiplier 2 5 1
Load/store unit 3 2 3 1
ROB entries = 7
Issue width = 3
r10=8, f12=2.5, R0=99
Mem[8]=4
lD f14, 0(r10)
aDd.d f15, f14, f12
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.in'
            path.write_text(text, encoding='utf-8')
            config, registers, memory, instructions = read_program(path)
            self.assertEqual(config.issue_width, 3)
            self.assertEqual(config.integer.count, 2)
            self.assertEqual(config.rob_entries, 7)
            self.assertEqual(registers['R0'], 0)
            self.assertEqual(Machine(config, registers, memory, instructions).run().registers['F15'], 6.5)
            path.write_text(text.replace('Issue width = 3', 'Issue width = 5'), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_program(path)
            path.write_text(text.replace('Issue width = 3\n', ''), encoding='utf-8')
            self.assertEqual(read_program(path)[0].issue_width, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
