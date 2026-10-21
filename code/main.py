"""Command line entry point for the assignment simulator."""
import argparse
import json
from pathlib import Path
from simulator import Machine, read_program


def main():
    parser = argparse.ArgumentParser(description='Tomasulo simulator with in-order issue widths 1-4')
    parser.add_argument('input', nargs='?', type=Path, default=Path(__file__).with_name('test_case.txt'))
    parser.add_argument('--issue-width', type=int, choices=range(1, 5))
    parser.add_argument('--max-cycles', type=int, default=100000)
    parser.add_argument('--json', type=Path, help='Write full results, including squashed instructions')
    args = parser.parse_args()
    try:
        config, registers, memory, instructions = read_program(args.input)
        if args.issue_width is not None:
            config.issue_width = args.issue_width
        machine = Machine(config, registers, memory, instructions).run(args.max_cycles)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(machine.format_output())
    if args.json:
        args.json.write_text(json.dumps(machine.as_dict(), indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
