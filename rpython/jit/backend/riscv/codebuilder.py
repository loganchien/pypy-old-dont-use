#!/usr/bin/env python

from rpython.jit.backend.llsupport.asmmemmgr import BlockBuilderMixin
from rpython.jit.backend.riscv import registers as r
from rpython.jit.backend.riscv.arch import (
    PC_REL_MAX, PC_REL_MIN, SINT12_IMM_MAX, SINT12_IMM_MIN)
from rpython.jit.backend.riscv.instruction_builder import (
    gen_all_instr_assemblers)
from rpython.jit.backend.riscv.instruction_util import (
    COND_BEQ, COND_BGE, COND_BGEU, COND_BLT, COND_BLTU, COND_BNE,
    check_imm_arg)
from rpython.rlib.objectmodel import we_are_translated
from rpython.rtyper.lltypesystem import rffi
from rpython.tool.udir import udir


_SINT32_MIN = -2**31
_SINT32_MAX = 2**31 - 1
_SINT64_MIN = -2**63
_SINT64_MAX = 2**63 - 1


class AbstractRISCVBuilder(object):
    def write32(self, word):
        self.writechar(chr(word & 0xff))
        self.writechar(chr((word >> 8) & 0xff))
        self.writechar(chr((word >> 16) & 0xff))
        self.writechar(chr((word >> 24) & 0xff))

    # NOP
    def NOP(self):
        self.ADDI(r.zero.value, r.zero.value, 0)

    # Move register
    def MV(self, rd, rs1):
        self.ADDI(rd, rs1, 0)

    # Move fp register (double)
    def FMV_D(self, rd, rs1):
        self.FSGNJ_D(rd, rs1, rs1)

    # Jump to a pc-relative offset (+/-1MB)
    def J(self, imm):
        self.JAL(r.zero.value, imm)

    # Jump to the address kept in the register
    def JR(self, rs1):
        self.JALR(r.zero.value, rs1, 0)

    # Return from a function
    def RET(self):
        self.JALR(r.zero.value, r.ra.value, 0)

    # Bitwise not
    def NOT(self, rd, rs1):
        self.XORI(rd, rs1, -1)

    # Integer negation (additive inverse)
    def NEG(self, rd, rs1):
        self.SUB(rd, r.zero.value, rs1)

    # Set equal to zero
    def SEQZ(self, rd, rs1):
        self.SLTIU(rd, rs1, 1)

    # Set not equal to zero
    def SNEZ(self, rd, rs1):
        self.SLTU(rd, r.zero.value, rs1)

    # Set less than zero
    def SLTZ(self, rd, rs1):
        self.SLT(rd, rs1, r.zero.value)

    # Set greater than zero
    def SGTZ(self, rd, rs1):
        self.SLT(rd, r.zero.value, rs1)

    # Floating point negation (additive inverse)
    def FNEG_D(self, rd, rs1):
        self.FSGNJN_D(rd, rs1, rs1)

    # Floating point absolute function
    def FABS_D(self, rd, rs1):
        self.FSGNJX_D(rd, rs1, rs1)

    # Branch if equal to zero
    def BEQZ(self, rs1, offset):
        self.BEQ(rs1, r.zero.value, offset)

    # Branch if not equal to zero
    def BNEZ(self, rs1, offset):
        self.BNE(rs1, r.zero.value, offset)

    # Branch if less than or equal to zero
    def BLEZ(self, rs1, offset):
        self.BGE(r.zero.value, rs1, offset)

    # Branch if greater than or equal to to zero
    def BGEZ(self, rs1, offset):
        self.BGE(rs1, r.zero.value, offset)

    # Branch if less than zero
    def BLTZ(self, rs1, offset):
        self.BLT(rs1, r.zero.value, offset)

    # Branch if greater than zero
    def BGTZ(self, rs1, offset):
        self.BLT(r.zero.value, rs1, offset)

    # Load an XLEN-bit integer from imm(rs1)
    def load_int(self, rd, rs1, imm):
        self.LD(rd, rs1, imm)

    # Store an XLEN-bit integer to imm(rs1)
    def store_int(self, rs2, rs1, imm):
        self.SD(rs2, rs1, imm)

    # Load an XLEN-bit integer from rs1+imm (imm can be a large constant)
    def load_int_from_base_plus_offset(self, rd, rs1, imm, tmp=-1):
        if tmp == -1:
            tmp = rd
        assert tmp != rs1
        if check_imm_arg(imm):
            self.load_int(rd, rs1, imm)
        else:
            self.load_int_imm(tmp, imm)
            self.ADD(tmp, tmp, rs1)
            self.load_int(rd, tmp, 0)

    # Store an XLEN-bit integer to rs1+imm (imm can be a large constant)
    def store_int_to_base_plus_offset(self, rs2, rs1, imm, tmp):
        assert tmp != rs2 and tmp != rs1
        if check_imm_arg(imm):
            self.store_int(rs2, rs1, imm)
        else:
            self.load_int_imm(tmp, imm)
            self.ADD(tmp, tmp, rs1)
            self.store_int(rs2, tmp, 0)

    # Atomic-swap XLEN-bit integer.  Load old value to rd and store new value
    # from rs2 to memory address 0(rs1).
    def atomic_swap_int(self, rd, rs2, rs1, acrl):
        self.AMOSWAP_D(rd, rs2, rs1, acrl)

    # Load-and-reserve XLEN-bit integer from memory address 0(rs1) to rd.
    def load_reserve_int(self, rd, rs1, acrl):
        self.LR_D(rd, rs1, acrl)

    # Store-conditional XLEN-bit integer rs2 to memory address 0(rs1) and write
    # zero to rd on success (conversely, non-zero to rd on failure).
    def store_conditional_int(self, rd, rs2, rs1, acrl):
        self.SC_D(rd, rs2, rs1, acrl)

    # Load an FLEN-bit float from imm(rs1)
    def load_float(self, rd, rs1, imm):
        self.FLD(rd, rs1, imm)

    # Store an FLEN-bit float to imm(rs1)
    def store_float(self, rs2, rs1, imm):
        self.FSD(rs2, rs1, imm)

    # Load a rffi.INT from imm(rs1)
    def load_rffi_int(self, rd, rs1, imm):
        # Note: On RV64 (LP64), rffi.INT is 32-bit signed integer.
        self.LW(rd, rs1, imm)

    # Store a rffi.INT to imm(rs1)
    def store_rffi_int(self, rs2, rs1, imm):
        self.SW(rs2, rs1, imm)

    # Load a rffi.INT from rs1+imm (imm can be a large constant)
    def load_rffi_int_from_base_plus_offset(self, rd, rs1, imm, tmp=-1):
        if tmp == -1:
            tmp = rd
        assert tmp != rs1
        if check_imm_arg(imm):
            self.load_rffi_int(rd, rs1, imm)
        else:
            self.load_int_imm(tmp, imm)
            self.ADD(tmp, tmp, rs1)
            self.load_rffi_int(rd, tmp, 0)

    # Store a rffi.INT to rs1+imm (imm can be a large constant)
    def store_rffi_int_to_base_plus_offset(self, rs2, rs1, imm, tmp):
        assert tmp != rs2 and tmp != rs1
        if check_imm_arg(imm):
            self.store_rffi_int(rs2, rs1, imm)
        else:
            self.load_int_imm(tmp, imm)
            self.ADD(tmp, tmp, rs1)
            self.store_rffi_int(rs2, tmp, 0)

    # Splits an immediate value (or a pc-relative offset) into an upper part
    # for the auipc/lui instruction and a lower part for the
    # load/store/jalr/addiw instructions.
    @staticmethod
    def split_imm32(offset):
        lower = offset & 0xfff
        if lower >= 0x800:
            lower -= 0x1000
            offset += 0x1000
        upper = ((offset >> 12) & 0xfffff)
        return (upper, lower)

    # Load an XLEN-bit integer from pc-relative offset
    def load_int_pc_rel(self, rd, offset):
        assert PC_REL_MIN <= offset <= PC_REL_MAX
        upper, lower = self.split_imm32(offset)
        self.AUIPC(rd, upper)
        self.load_int(rd, rd, lower)

    # Load an FLEN-bit float from pc-relative offset
    def load_float_pc_rel(self, rd, offset, scratch_reg=r.x31.value):
        assert PC_REL_MIN <= offset <= PC_REL_MAX
        upper, lower = self.split_imm32(offset)
        self.AUIPC(scratch_reg, upper)
        self.load_float(rd, scratch_reg, lower)

    # Long jump (+/-2GB) to a pc-relative offset
    def jalr_pc_rel(self, rd, offset):
        assert int(rd) != 0
        assert PC_REL_MIN <= offset <= PC_REL_MAX
        upper, lower = self.split_imm32(offset)
        self.AUIPC(rd, upper)
        self.JALR(rd, rd, lower)

    # Absolute jump
    def jal_abs(self, rd, abs_addr):
        scratch_reg = r.x31
        self.load_int_imm(scratch_reg.value, abs_addr)
        self.JALR(rd, scratch_reg.value, 0)

    # Load an integer constant to a register
    def load_int_imm(self, rd, imm):
        assert _SINT64_MIN <= imm <= _SINT64_MAX
        self._load_int_imm(rd, imm)

    def _load_int_imm(self, rd, imm):
        if SINT12_IMM_MIN <= imm <= SINT12_IMM_MAX:
            self.ADDI(rd, r.zero.value, imm)
            return
        elif _SINT32_MIN <= imm <= _SINT32_MAX:
            upper, lower = self.split_imm32(imm)
            self.LUI(rd, upper)
            if lower:
                self.ADDIW(rd, rd, lower)
            return

        # Implement trailing zeros with slli
        shamt = 0
        while imm & 0x1 == 0:
            shamt += 1
            imm >>= 1
        if shamt:
            self._load_int_imm(rd, imm)
            self.SLLI(rd, rd, shamt)
            return

        # Split lower 12-bit
        lower = imm & 0xfff
        if lower >= 0x800:
            lower -= 0x1000
            imm += 0x1000

        # Move trailing zeros to shamt
        imm >>= 12
        shamt = 12
        while imm & 0x1 == 0:
            shamt += 1
            imm >>= 1

        self._load_int_imm(rd, imm)
        self.SLLI(rd, rd, shamt)
        self.ADDI(rd, rd, lower)

gen_all_instr_assemblers(AbstractRISCVBuilder)

BRANCH_BUILDER = {
    COND_BEQ: AbstractRISCVBuilder.BEQ,
    COND_BNE: AbstractRISCVBuilder.BNE,

    COND_BGE: AbstractRISCVBuilder.BGE,
    COND_BLT: AbstractRISCVBuilder.BLT,

    COND_BGEU: AbstractRISCVBuilder.BGEU,
    COND_BLTU: AbstractRISCVBuilder.BLTU,
}


class OverwritingBuilder(AbstractRISCVBuilder):
    def __init__(self, cb, start, size):
        AbstractRISCVBuilder.__init__(self)
        self.cb = cb
        self.index = start
        self.start = start
        self.end = start + size

    def currpos(self):
        return self.index

    def writechar(self, char):
        assert self.index <= self.end
        self.cb.overwrite(self.index, char)
        self.index += 1


class InstrBuilder(BlockBuilderMixin, AbstractRISCVBuilder):
    def __init__(self):
        AbstractRISCVBuilder.__init__(self)
        self.init_block_builder()

        # ops_offset[None] represents the beginning of the code after the last
        # op (i.e., the tail of the loop)
        self.ops_offset = {}

    def mark_op(self, op):
        self.ops_offset[op] = self.get_relative_pos()

    def _dump_trace(self, addr, name, formatter=-1):
        if not we_are_translated():
            if formatter != -1:
                name = name % formatter
            dir = udir.ensure('asm', dir=True)
            f = dir.join(name).open('wb')
            data = rffi.cast(rffi.CCHARP, addr)
            for i in range(self.get_relative_pos()):
                f.write(data[i])
            f.close()
