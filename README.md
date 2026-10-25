# Tomasulo assignment

Run the simulator with Python 3.10 or newer. It only uses the Python standard
library, so no extra packages are needed.

From this directory:

```sh
python code/main.py
python code/main.py tests/vector_loop.in --issue-width 4 --json results/run.json
python tests/test_simulator.py
python run_benchmarks.py
```

With no arguments, `code/main.py` runs `code/test_case.txt` with issue width 1.
The input file contains the hardware settings, initial register and memory
values, and the instructions. The issue width can be set to 1, 2, 3, or 4 in
the input file, or overridden with `--issue-width`. If it is not specified, it
defaults to 1.

The input format is case-insensitive. Operands can be separated with spaces or
commas, and comments are allowed.

The main implementation is in `code/simulator.py`. It includes register
renaming, a reorder buffer, reservation stations, pipelined FP units,
unpipelined integer units, a load/store queue, one memory port, one CDB, one
commit per cycle, and speculative branches. The assignment operations,
including `Beq` and `Bne`, are supported.

Memory uses byte addresses in a 256-byte memory. Locations that are not
initialized have value zero. Multiple load/store units can generate addresses,
but they still share the same memory port.

`Tomasulo_Implementation_Report.pdf` describes the implementation, timing rules,
and benchmark results. The files in `tests/` are standalone test cases. The files
under `results/` contain the benchmark output; `summary.csv` compares all four
issue widths. The JSON files also include instructions that were squashed.

## Starter compatibility

The original Qing (2017) code and input files are kept in `starter/` for
reference. `main.py` uses the newer engine in `simulator.py`; the older stage
files in `code/` are not part of the active execution path.

The original version had a few problems that affected multiple issue,
including hardcoded initialization, mutable namedtuple data, byte-based branch
offsets, and serialized branch execution. These parts were rewritten in the
new engine while keeping the starter files available for comparison.

The supplied test contains `Bne R1 R0 -12`. The old code treated this as a byte
offset, but the assignment uses word offsets, so `code/test_case.txt` uses `-3`
instead. This preserves the original control flow using:

```text
target = PC + 4 + (offset << 2)
```

This branch goes back to the integer updates rather than the beginning of the
memory loop. `tests/vector_loop.in` uses `-8` and runs the complete
three-iteration load/multiply/add/store loop. The original byte-offset input is
still in `starter/test_case.txt`.

`code/code.in` only contains instructions and does not provide initialization
values, so `code/test_case.txt` is the easier supplied example to run.

## Timing and limitations

Issue stops at the first ROB, reservation-station, or load/store-queue stall.
Instructions issued in the same cycle see earlier register-renaming updates in
that bundle. Results become available to execution on the following cycle.
Loads go to MEM after EX and to WB after MEM. There is one result broadcast and
one commit per cycle.

Stores access memory only when they reach the ROB head and commit on their last
memory cycle. A branch resolved in cycle `n` resumes correct fetching in cycle
`n+2`. Loads that conflict with an older store whose address is resolved late
are replayed. Invalid load addresses are checked at retirement, so wrong-path
loads can be discarded safely.

Values are represented as Python integers and floats. The simulator does not
model bit-level overflow or single-precision IEEE-754 rounding. Memory words
are stored by their byte starting address, matching the starter; overlapping
unaligned words are not modeled as shared individual bytes.

The simulator keeps dynamic instruction history for the reports, so very long
programs use more host memory. `--max-cycles` can be used to stop an infinite
loop.
