"""Generate reproducible benchmark inputs and run each at issue widths 1-4."""
import copy
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
sys.path.insert(0, str(ROOT / 'tests'))
from simulator import Machine, read_program
from test_simulator import reference

HEADER = '''# RS, EX cycles, [MEM cycles], FU count
Integer adder 8 2 1
FP adder 8 3 1
FP multiplier 8 6 1
Load/store unit 8 1 3 1
ROB entries = 32
F1=2.0, F2=3.0, F3=4.0
'''
CASES = {
    'independent': HEADER + '''Add R1 R0 R0
Add.d F4 F1 F2
Mult.d F5 F1 F2
Add R2 R0 R0
Add.d F6 F1 F2
Mult.d F7 F1 F2
Add R3 R0 R0
Add.d F8 F1 F2
Mult.d F9 F1 F2
Add R4 R0 R0
Add.d F10 F1 F2
Mult.d F11 F1 F2
''',
    'dependencies': HEADER + '''Mult.d F4 F1 F2
Add.d F5 F4 F3
Add.d F4 F1 F1
Sub.d F6 F5 F4
Addi R1 R0 5
Addi R1 R1 2
Add R2 R1 R1
Addi R0 R2 100
''',
    'forwarding': HEADER + '''Mult.d F10 F1 F2
Sd F1 0(R0)
Sd F2 4(R0)
Ld F4 0(R0)
Ld F5 4(R0)
Add.d F6 F4 F5
''',
    'branch_recovery': HEADER + '''Beq R0 R0 2
Sd F1 0(R0)
Addi R1 R0 999
Addi R1 R0 7
Addi R2 R1 1
''',
    'memory_replay': HEADER.replace('Integer adder 8 2 1', 'Integer adder 8 7 1') + '''Mem[8]=99
Addi R10 R0 8
Sd F1 0(R10)
Ld F4 8(R0)
Add.d F5 F4 F4
''',
    'vector_loop': '''Integer adder 4 1 1
FP adder 3 4 1
FP multiplier 2 15 1
Load/store unit 5 1 5 1
ROB entries = 64
R1=12, R2=32, F20=3.0
Mem[4]=3, Mem[8]=2, Mem[12]=1, Mem[24]=6, Mem[28]=5, Mem[32]=4
Ld F2 0(R1)
Mult.d F4 F2 F20
Ld F6 0(R2)
Add.d F6 F4 F6
Sd F6 0(R2)
Addi R1 R1 -4
Addi R2 R2 -4
Bne R1 R0 -8
Add.d F20 F2 F2
''',
    'structural_stalls': HEADER.replace('Integer adder 8 2 1', 'Integer adder 1 4 1') + '''Add R1 R0 R0
Add R2 R0 R0
Add.d F4 F1 F2
Mult.d F5 F1 F2
''',
}


def main():
    inputs, output = ROOT / 'tests', ROOT / 'results'
    inputs.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    paths = [ROOT / 'code' / 'test_case.txt']
    for name, text in CASES.items():
        path = inputs / (name + '.in')
        path.write_text(text, encoding='utf-8')
        paths.append(path)
    rows = []
    for path in paths:
        config, registers, memory, instructions = read_program(path)
        expected_regs, expected_mem, expected_count = reference(instructions, registers, memory)
        baseline = None
        for width in range(1, 5):
            settings = copy.deepcopy(config)
            settings.issue_width = width
            machine = Machine(settings, registers, memory, instructions).run()
            assert machine.registers == expected_regs, (path, width, 'register mismatch')
            assert {k: v for k, v in machine.memory.items() if v != 0} == expected_mem, (path, width, 'memory mismatch')
            assert len(machine.committed) == expected_count
            baseline = baseline or machine.cycle
            name = path.stem + '_width' + str(width)
            (output / (name + '.txt')).write_text(machine.format_output() + '\n', encoding='utf-8')
            (output / (name + '.json')).write_text(json.dumps(machine.as_dict(), indent=2) + '\n', encoding='utf-8')
            rows.append({'case': path.stem, 'issue_width': width, 'cycles': machine.cycle,
                         'committed': len(machine.committed), 'speedup': baseline / machine.cycle,
                         'mispredictions': machine.mispredictions, 'memory_replays': machine.memory_replays})
        print(f'{path.stem}: cycles ' + ', '.join(str(row['cycles']) for row in rows[-4:]), flush=True)
    with (output / 'summary.csv').open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


if __name__ == '__main__':
    main()
