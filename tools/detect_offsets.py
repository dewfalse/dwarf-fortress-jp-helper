#!/usr/bin/env python3
"""Detect DF text-related function RVAs from a Windows x64 executable.

The detector intentionally uses control-flow and operand structure instead of
fixed RVAs. It is designed for nearby Dwarf Fortress releases built with MSVC.
It refuses to emit TOML when the best candidate is ambiguous.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

try:
    import pefile
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    from capstone.x86 import (
        X86_INS_CALL,
        X86_INS_CMP,
        X86_INS_MOV,
        X86_INS_MOVSXD,
        X86_OP_IMM,
        X86_OP_MEM,
        X86_OP_REG,
        X86_REG_R8B,
        X86_REG_R9D,
        X86_REG_RBX,
        X86_REG_RCX,
        X86_REG_RDX,
    )
except ImportError as exc:
    print(
        "Missing dependency. Install with: pip install pefile capstone",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


@dataclasses.dataclass(frozen=True)
class FunctionRange:
    begin: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.begin


@dataclasses.dataclass
class Candidate:
    function: FunctionRange
    score: int
    reasons: list[str]


OFFSET_NAMES = (
    "addst",
    "addst_top",
    "addst_flag",
    "rich_text_parse",
    "rich_text_render",
)


class Image:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        self.pe = pefile.PE(str(path), fast_load=False)
        if self.pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError("Only Windows x64 PE files are supported")

        self.image_base = self.pe.OPTIONAL_HEADER.ImageBase
        self.text = next(
            section
            for section in self.pe.sections
            if section.Name.rstrip(b"\0") == b".text"
        )
        self.text_rva = self.text.VirtualAddress
        self.text_data = self.data[
            self.text.PointerToRawData :
            self.text.PointerToRawData + self.text.SizeOfRawData
        ]
        self.functions = sorted(
            {
                FunctionRange(entry.struct.BeginAddress, entry.struct.EndAddress)
                for entry in self.pe.DIRECTORY_ENTRY_EXCEPTION
                if self.text_rva
                <= entry.struct.BeginAddress
                < self.text_rva + self.text.Misc_VirtualSize
                and entry.struct.EndAddress > entry.struct.BeginAddress
            },
            key=lambda item: (item.begin, item.end),
        )
        self.function_starts = [item.begin for item in self.functions]

        self.disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
        self.disassembler.detail = True
        self._instruction_cache: dict[FunctionRange, list] = {}

    @property
    def timestamp(self) -> int:
        return self.pe.FILE_HEADER.TimeDateStamp

    def bytes_at(self, rva: int, size: int) -> bytes:
        offset = self.pe.get_offset_from_rva(rva)
        return self.data[offset : offset + size]

    def instructions(self, function: FunctionRange) -> list:
        cached = self._instruction_cache.get(function)
        if cached is None:
            cached = list(
                self.disassembler.disasm(
                    self.bytes_at(function.begin, function.size),
                    self.image_base + function.begin,
                )
            )
            self._instruction_cache[function] = cached
        return cached

    def containing_function(self, rva: int) -> FunctionRange | None:
        index = bisect.bisect_right(self.function_starts, rva) - 1
        while index >= 0 and self.functions[index].begin <= rva:
            function = self.functions[index]
            if function.begin <= rva < function.end:
                return function
            if function.end <= rva:
                break
            index -= 1
        return None

    def direct_calls(self, function: FunctionRange) -> list[int]:
        calls: list[int] = []
        for instruction in self.instructions(function):
            if (
                instruction.id == X86_INS_CALL
                and instruction.operands
                and instruction.operands[0].type == X86_OP_IMM
            ):
                calls.append(instruction.operands[0].imm - self.image_base)
        return calls

    def direct_callers(self, target_rva: int) -> list[FunctionRange]:
        callers: set[FunctionRange] = set()
        text = self.text_data
        for index in range(len(text) - 5):
            if text[index] != 0xE8:
                continue
            displacement = int.from_bytes(
                text[index + 1 : index + 5], "little", signed=True
            )
            call_rva = self.text_rva + index
            if call_rva + 5 + displacement != target_rva:
                continue
            function = self.containing_function(call_rva)
            if function:
                callers.add(function)
        return sorted(callers, key=lambda item: item.begin)

    def functions_containing_bytes(self, pattern: bytes) -> set[FunctionRange]:
        result: set[FunctionRange] = set()
        start = 0
        while True:
            index = self.text_data.find(pattern, start)
            if index < 0:
                break
            function = self.containing_function(self.text_rva + index)
            if function:
                result.add(function)
            start = index + 1
        return result


def memory_displacements(instructions: Iterable) -> Counter[int]:
    result: Counter[int] = Counter()
    for instruction in instructions:
        for operand in instruction.operands:
            if operand.type == X86_OP_MEM:
                result[operand.mem.disp] += 1
    return result


def has_register_move(instructions: Iterable, destination: int, source: int) -> bool:
    for instruction in instructions:
        if instruction.id not in (X86_INS_MOV, X86_INS_MOVSXD):
            continue
        if len(instruction.operands) < 2:
            continue
        left, right = instruction.operands[:2]
        if (
            left.type == X86_OP_REG
            and right.type == X86_OP_REG
            and left.reg == destination
            and right.reg == source
        ):
            return True
    return False


def score_addst_candidate(image: Image, function: FunctionRange) -> Candidate:
    instructions = image.instructions(function)
    displacements = memory_displacements(instructions)
    calls = image.direct_calls(function)
    reasons: list[str] = []
    score = 0

    if 0x80 <= function.size <= 0x180:
        score += 2
        reasons.append("small character-loop function")
    if displacements[0x84] >= 2:
        score += 5
        reasons.append("reads graphicst::screenx (+0x84)")
    if displacements[0x10] >= 1:
        score += 2
        reasons.append("reads std::string length (+0x10)")
    if 4 <= len(calls) <= 6:
        score += 2
        reasons.append(f"{len(calls)} direct calls")
    if any(
        instruction.id == X86_INS_CMP
        and any(
            operand.type == X86_OP_MEM and operand.mem.disp == 0x84
            for operand in instruction.operands
        )
        for instruction in instructions
    ):
        score += 2
        reasons.append("screen-bound comparison")
    if any(instruction.mnemonic == "movzx" for instruction in instructions):
        score += 1
        reasons.append("loads one byte per iteration")

    return Candidate(function, score, reasons)


def detect_addst_family(image: Image) -> tuple[Candidate, Candidate, Candidate]:
    # All three functions begin by checking std::string::len at [rdx+0x10].
    seed_patterns = (
        b"\x48\x83\x7a\x10\x00",  # cmp qword ptr [rdx+10h], 0
        b"\x48\x8b\x4a\x10",      # mov rcx, qword ptr [rdx+10h]
    )
    functions: set[FunctionRange] = set()
    for pattern in seed_patterns:
        functions.update(image.functions_containing_bytes(pattern))

    candidates = [
        score_addst_candidate(image, function)
        for function in functions
        if 0x70 <= function.size <= 0x200
    ]
    candidates = sorted(candidates, key=lambda item: item.score, reverse=True)
    strong = [item for item in candidates if item.score >= 12]

    normal: list[Candidate] = []
    top: list[Candidate] = []
    flag: list[Candidate] = []
    for candidate in strong:
        instructions = image.instructions(candidate.function)
        # Capstone represents "mov r8b, 1" with an immediate.
        sets_r8b_one = any(
            instruction.id == X86_INS_MOV
            and len(instruction.operands) >= 2
            and instruction.operands[0].type == X86_OP_REG
            and instruction.operands[0].reg == X86_REG_R8B
            and instruction.operands[1].type == X86_OP_IMM
            and instruction.operands[1].imm == 1
            for instruction in instructions
        )
        copies_r9d = has_register_move(instructions, X86_REG_RBX, X86_REG_R9D)
        forwards_r9d = any(
            instruction.id == X86_INS_MOV
            and instruction.operands[0].type == X86_OP_REG
            and instruction.operands[0].reg == X86_REG_R9D
            for instruction in instructions
        )

        if not sets_r8b_one and forwards_r9d:
            candidate.reasons.append("forwards per-character flag in r9d")
            candidate.score += 3
            flag.append(candidate)
        elif sets_r8b_one and copies_r9d:
            candidate.reasons.append("uses fourth argument as leading-space count")
            candidate.score += 3
            normal.append(candidate)
        elif sets_r8b_one and not copies_r9d:
            candidate.reasons.append("top-layer character writer")
            candidate.score += 3
            top.append(candidate)

    return (
        choose_unique("addst", normal),
        choose_unique("addst_top", top),
        choose_unique("addst_flag", flag),
    )


def score_render_candidate(
    image: Image, function: FunctionRange, addst_rva: int
) -> Candidate:
    instructions = image.instructions(function)
    displacements = memory_displacements(instructions)
    calls = image.direct_calls(function)
    reasons: list[str] = []
    score = 0

    if 0x180 <= function.size <= 0x500:
        score += 3
        reasons.append("medium-sized renderer")
    if calls.count(addst_rva) == 1:
        score += 8
        reasons.append("calls addst exactly once")
    if displacements[0x180] >= 2:
        score += 7
        reasons.append("walks layout pointer at +0x180")
    token_offsets = sum(
        1 for offset in (0x20, 0x21, 0x22, 0x24, 0x28, 0x2C)
        if displacements[offset]
    )
    score += token_offsets
    if token_offsets >= 5:
        reasons.append("reads rich-text token fields")
    widget_offsets = sum(
        1 for offset in (0x10, 0x14, 0x18, 0x1C, 0x150)
        if displacements[offset]
    )
    score += widget_offsets
    if widget_offsets >= 4:
        reasons.append("reads widget bounds and scroll position")

    return Candidate(function, score, reasons)


def detect_rich_text_render(image: Image, addst_rva: int) -> Candidate:
    candidates = [
        score_render_candidate(image, function, addst_rva)
        for function in image.direct_callers(addst_rva)
        if function.size <= 0x800
    ]
    return choose_unique(
        "rich_text_render",
        [candidate for candidate in candidates if candidate.score >= 20],
    )


def score_parse_candidate(
    image: Image, function: FunctionRange, render_rva: int
) -> Candidate:
    instructions = image.instructions(function)
    calls = image.direct_calls(function)
    call_counts = Counter(calls)
    displacements = memory_displacements(instructions)
    reasons: list[str] = []
    score = 0

    distance = render_rva - function.end
    if 0x1000 <= function.size <= 0x6000:
        score += 6
        reasons.append("large rich-text parser-sized function")
    if 0 <= distance <= 0x4000:
        score += 5
        reasons.append(f"located {distance:#x} bytes before renderer")
    if len(calls) >= 80:
        score += 4
        reasons.append(f"complex parser call graph ({len(calls)} calls)")
    local_helpers = {
        target: count
        for target, count in call_counts.items()
        if function.end <= target < render_rva
    }
    repeated_helper_count = max(local_helpers.values(), default=0)
    if repeated_helper_count >= 8:
        score += 5
        reasons.append(
            f"repeated token-flush helper ({repeated_helper_count} calls)"
        )
    token_fields = sum(
        displacements[offset]
        for offset in (0x20, 0x21, 0x22, 0x24, 0x28, 0x2C)
    )
    if token_fields >= 20:
        score += 4
        reasons.append("heavily manipulates rich-text token fields")
    if len(instructions) >= 1000:
        score += 2
        reasons.append("large state machine")

    return Candidate(function, score, reasons)


def detect_rich_text_parse(image: Image, render_rva: int) -> Candidate:
    nearby = [
        function
        for function in image.functions
        if function.end <= render_rva
        and render_rva - function.end <= 0x10000
        and function.size >= 0x800
    ]
    candidates = [
        score_parse_candidate(image, function, render_rva)
        for function in nearby
    ]
    return choose_unique(
        "rich_text_parse",
        [candidate for candidate in candidates if candidate.score >= 18],
    )


def choose_unique(name: str, candidates: list[Candidate]) -> Candidate:
    candidates = sorted(
        candidates,
        key=lambda item: (item.score, -item.function.begin),
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"{name}: no candidate passed the confidence threshold")
    best = candidates[0]
    if len(candidates) > 1 and candidates[1].score >= best.score - 1:
        summary = ", ".join(
            f"{item.function.begin:#x} (score {item.score})"
            for item in candidates[:5]
        )
        raise RuntimeError(f"{name}: ambiguous candidates: {summary}")
    return best


def format_candidate(name: str, candidate: Candidate) -> str:
    reasons = "; ".join(candidate.reasons)
    return (
        f"{name:17} = {candidate.function.begin:#010x}  "
        f"score={candidate.score:2d}  {reasons}"
    )


def render_toml(image: Image, detected: dict[str, Candidate]) -> str:
    lines = [
        "[metadata]",
        'name = "dfjp auto-detected hook offsets"',
        "version = \"unknown\"",
        f"checksum = 0x{image.timestamp:08X}",
        "",
        "[offsets]",
    ]
    for name in OFFSET_NAMES:
        lines.append(f"{name} = 0x{detected[name].function.begin:X}")
    return "\n".join(lines) + "\n"


def output_matches_timestamp(path: Path, timestamp: int) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False

    checksum_match = re.search(
        r"^checksum\s*=\s*(0x[0-9A-Fa-f]+|\d+)", text, re.MULTILINE
    )
    if not checksum_match or int(checksum_match.group(1), 0) != timestamp:
        return False

    return all(
        re.search(
            rf"^{re.escape(name)}\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
            text,
            re.MULTILINE,
        )
        for name in OFFSET_NAMES
    )


def detect_offsets(image: Image) -> dict[str, Candidate]:
    addst, addst_top, addst_flag = detect_addst_family(image)
    rich_text_render = detect_rich_text_render(image, addst.function.begin)
    rich_text_parse = detect_rich_text_parse(image, rich_text_render.function.begin)
    return {
        "addst": addst,
        "addst_top": addst_top,
        "addst_flag": addst_flag,
        "rich_text_parse": rich_text_parse,
        "rich_text_render": rich_text_render,
    }


def detect_offsets_from_executable(executable: Path) -> tuple[Image, dict[str, Candidate]]:
    image = Image(executable)
    return image, detect_offsets(image)


def ensure_offsets_file(executable: Path, output: Path) -> bool:
    image = Image(executable)
    if output_matches_timestamp(output, image.timestamp):
        return False

    detected = detect_offsets(image)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_toml(image, detected), encoding="utf-8")
    return True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write offsets TOML after all confidence checks pass",
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="Skip detection when --output already matches the executable timestamp",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        image = Image(args.executable)
        if args.ensure and args.output and output_matches_timestamp(args.output, image.timestamp):
            print(
                f"Offsets are current: {args.output} "
                f"(checksum 0x{image.timestamp:08X})"
            )
            return 0

        detected = detect_offsets(image)
    except (OSError, ValueError, RuntimeError, pefile.PEFormatError) as exc:
        print(f"Detection failed: {exc}", file=sys.stderr)
        return 1

    print(f"PE timestamp       = 0x{image.timestamp:08X}")
    for name in OFFSET_NAMES:
        print(format_candidate(name, detected[name]))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_toml(image, detected), encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
