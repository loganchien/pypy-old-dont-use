#!/usr/bin/env python

from rpython.jit.backend.llsupport import jitframe
from rpython.jit.backend.llsupport.asmmemmgr import MachineDataBlockWrapper
from rpython.jit.backend.model import CompiledLoopToken
from rpython.jit.backend.riscv import registers as r
from rpython.jit.backend.riscv.arch import (
    ABI_STACK_ALIGN, FLEN, JITFRAME_FIXED_SIZE, SCRATCH_STACK_SLOT_SIZE, XLEN)
from rpython.jit.backend.riscv.codebuilder import InstrBuilder
from rpython.jit.backend.riscv.instruction_util import (
    can_fuse_into_compare_and_branch, check_simm21_arg)
from rpython.jit.backend.riscv.opassembler import (
    OpAssembler, asm_guard_operations, asm_operations)
from rpython.jit.backend.riscv.regalloc import (
    Regalloc, regalloc_guard_operations, regalloc_operations)
from rpython.jit.codewriter.effectinfo import EffectInfo
from rpython.jit.metainterp.history import AbstractFailDescr, FLOAT
from rpython.jit.metainterp.resoperation import rop
from rpython.rlib.debug import debug_print, debug_start, debug_stop
from rpython.rlib.jit import AsmInfo
from rpython.rlib.objectmodel import we_are_translated
from rpython.rlib.rarithmetic import r_uint
from rpython.rlib.rjitlog import rjitlog as jl
from rpython.rtyper.lltypesystem import lltype, rffi


class AssemblerRISCV(OpAssembler):
    def __init__(self, cpu, translate_support_code=False):
        OpAssembler.__init__(self, cpu, translate_support_code)
        self.failure_recovery_code = [0, 0, 0, 0]
        self.wb_slowpath = [0, 0, 0, 0, 0]

    def assemble_loop(self, jd_id, unique_id, logger, loopname, inputargs,
                      operations, looptoken, log):
        if not we_are_translated():
            # Arguments should be unique
            assert len(set(inputargs)) == len(inputargs)

        clt = CompiledLoopToken(self.cpu, looptoken.number)
        clt._debug_nbargs = len(inputargs)
        looptoken.compiled_loop_token = clt

        self.setup(looptoken)

        frame_info = self.datablockwrapper.malloc_aligned(
            jitframe.JITFRAMEINFO_SIZE, alignment=XLEN)
        clt.frame_info = rffi.cast(jitframe.JITFRAMEINFOPTR, frame_info)
        clt.frame_info.clear()

        #if log:
        #    operations = self._inject_debugging_code(looptoken, operations,
        #                                             'e', looptoken.number)

        regalloc = Regalloc(self)
        allgcrefs = []
        operations = regalloc.prepare_loop(inputargs, operations, looptoken,
                                           allgcrefs)
        self.reserve_gcref_table(allgcrefs)
        function_pos = self.mc.get_relative_pos()

        self._call_header_with_stack_check()

        loop_head = self.mc.get_relative_pos()
        looptoken._ll_loop_code = loop_head

        frame_depth_no_fixed_size = self._assemble(regalloc, inputargs,
                                                   operations)
        self.update_frame_depth(frame_depth_no_fixed_size + JITFRAME_FIXED_SIZE)

        size_excluding_failure_stuff = self.mc.get_relative_pos()

        self.write_pending_failure_recoveries()

        full_size = self.mc.get_relative_pos()
        rawstart = self.materialize_loop(looptoken)
        looptoken._ll_function_addr = rawstart + function_pos

        self.patch_gcref_table(looptoken, rawstart)
        self.process_pending_guards(rawstart)
        self.fixup_target_tokens(rawstart)

        if log and not we_are_translated():
            self.mc._dump_trace(rawstart, 'loop.asm')

        ops_offset = self.mc.ops_offset

        #if logger:
        #    log = logger.log_trace(jl.MARK_TRACE_ASM, None, self.mc)
        #    log.write(inputargs, operations, ops_offset=ops_offset)
        #
        #    if logger.logger_ops:
        #        logger.logger_ops.log_loop(inputargs, operations, 0,
        #                                   'rewritten', name=loopname,
        #                                   ops_offset=ops_offset)

        self.teardown()

        debug_start('jit-backend-addr')
        debug_print('Loop %d (%s) has address 0x%x to 0x%x (bootstrap 0x%x)' % (
            looptoken.number, loopname,
            r_uint(rawstart + loop_head),
            r_uint(rawstart + size_excluding_failure_stuff),
            r_uint(rawstart + function_pos)))
        debug_print('       gc table: 0x%x' % r_uint(rawstart))
        debug_print('       function: 0x%x' % r_uint(rawstart + function_pos))
        debug_print('         resops: 0x%x' % r_uint(rawstart + loop_head))
        debug_print('       failures: 0x%x' % r_uint(rawstart +
                                                 size_excluding_failure_stuff))
        debug_print('            end: 0x%x' % r_uint(rawstart + full_size))
        debug_stop('jit-backend-addr')

        return AsmInfo(ops_offset, rawstart + loop_head,
                       size_excluding_failure_stuff - loop_head)

    def _assemble(self, regalloc, inputargs, operations):
        # Fill in the frame location hints so that we can reduce stack-to-stack
        # data movement in `remap_frame_layout_mixed`.
        regalloc.compute_hint_frame_locations(operations)

        # Visit all operations and regalloc/assemble the operations.
        self._walk_operations(inputargs, operations, regalloc)
        frame_depth = regalloc.get_final_frame_depth()

        # If the jump target (of the current loop) requires larger frame,
        # update the frame depth.
        jump_target_descr = regalloc.jump_target_descr
        if jump_target_descr is not None:
            tgt_depth = jump_target_descr._riscv_clt.frame_info.jfi_frame_depth
            target_frame_depth = tgt_depth - JITFRAME_FIXED_SIZE
            frame_depth = max(frame_depth, target_frame_depth)

        return frame_depth

    def _walk_operations(self, inputargs, operations, regalloc):
        self._regalloc = regalloc
        regalloc.operations = operations

        while regalloc.position() < len(operations) - 1:
            regalloc.next_instruction()
            i = regalloc.position()
            op = operations[i]
            self.mc.mark_op(op)
            opnum = op.getopnum()

            if rop.has_no_side_effect(opnum) and op not in regalloc.longevity:
                # If this op does not have side effects and its result is
                # unused, it is safe to ignore this op.
                pass
            elif not we_are_translated() and op.getopnum() == rop.FORCE_SPILL:
                regalloc.force_spill_var(op.getarg(0))
            elif (i < len(operations) - 1 and
                  ((can_fuse_into_compare_and_branch(opnum) and
                    regalloc.next_op_can_accept_cc(operations, i)) or
                   (op.is_ovf() and
                    rop.is_guard_overflow(operations[i + 1].getopnum())))):
                guard_op = operations[i + 1]  # guard_* or cond_call*
                guard_num = guard_op.getopnum()
                arglocs, guard_branch_inst = \
                        regalloc_guard_operations[guard_num](regalloc, op,
                                                             guard_op)
                if arglocs is not None:
                    asm_guard_operations[guard_num](self, op, guard_op, arglocs,
                                                    guard_branch_inst)
                regalloc.next_instruction()  # Advance one more
                # Free argument vars of the guard op (if no longer used).
                if guard_op.is_guard():
                    regalloc.possibly_free_vars(guard_op.getfailargs())
                regalloc.possibly_free_vars_for_op(guard_op)
                # Free the return var of the guard op (if no longer used).
                regalloc.possibly_free_var(guard_op)
            elif (rop.is_call_may_force(op.getopnum()) or
                  rop.is_call_release_gil(op.getopnum()) or
                  rop.is_call_assembler(op.getopnum())):
                guard_op = operations[i + 1]
                guard_num = guard_op.getopnum()
                assert guard_num in (rop.GUARD_NOT_FORCED,
                                     rop.GUARD_NOT_FORCED_2)

                # `arglocs` contains the locations for `op` and `guard_op`.
                # The first `num_arglocs` locations are for `op` and the
                # remainings are for `guard_op`.
                arglocs, num_arglocs = \
                        regalloc_guard_operations[guard_num](regalloc, op,
                                                             guard_op)
                if arglocs is not None:
                    asm_guard_operations[guard_num](self, op, guard_op, arglocs,
                                                    num_arglocs)
                regalloc.next_instruction()  # Advance one more

                # Free argument vars of the guard op (if no longer used).
                regalloc.possibly_free_vars(guard_op.getfailargs())
                regalloc.possibly_free_vars_for_op(guard_op)
            else:
                arglocs = regalloc_operations[opnum](regalloc, op)
                if arglocs is not None:
                    asm_operations[opnum](self, op, arglocs)

            # Free argument vars of the op (if no longer used).
            regalloc.possibly_free_vars_for_op(op)
            if rop.is_guard(opnum):
                regalloc.possibly_free_vars(op.getfailargs())

            # Free the return var of the op (if no longer used).
            #
            # Note: This can happen when we want the side-effect of an op (e.g.
            # `call_assembler_i` or `call_i`) but want to discard the returned
            # value.
            if op.type != 'v':
                regalloc.possibly_free_var(op)

            regalloc.free_temp_vars()
            regalloc._check_invariants()

        if not we_are_translated():
            self.mc.EBREAK()
        self.mc.mark_op(None)  # End of the loop
        regalloc.operations = None

    def _call_header_with_stack_check(self):
        self._call_header()

    def _call_header(self):
        self._push_callee_save_regs_to_stack(self.mc)

        # Save the thread local address to tls[0].
        self.saved_threadlocal_addr = 0 * XLEN
        self.mc.store_int(r.x11.value, r.sp.value, 0 * XLEN)

        self.mc.MV(r.jfp.value, r.x10.value)

    def _call_footer(self, mc):
        mc.MV(r.x10.value, r.jfp.value)
        self._pop_callee_save_regs_from_stack(mc)
        mc.RET()

    def _calculate_callee_save_area_size(self):
        # Extra thread local storage (see. riscv/callbuiler.py)
        #
        # tls[0 * XLEN]: saved_threadlocal_addr (_call_header)
        tls_size = XLEN * 1

        core_reg_begin = tls_size
        core_reg_size = XLEN * len(r.callee_saved_registers_except_ra_sp_fp)

        fp_reg_begin = core_reg_begin + core_reg_size
        fp_reg_begin = (fp_reg_begin + FLEN - 1) // FLEN * FLEN
        fp_reg_size = FLEN * len(r.callee_saved_fp_registers)

        # fp = old_sp
        # frame_record[0 * XLEN] (or fp[-2 * XLEN]): fp (old)
        # frame_record[1 * XLEN] (or fp[-1 * XLEN]): ra
        frame_record_begin = fp_reg_begin + fp_reg_size
        frame_record_begin = (frame_record_begin + XLEN - 1) // XLEN * XLEN
        frame_record_size = 2 * XLEN

        area_size = frame_record_begin + frame_record_size
        area_size = ((area_size + ABI_STACK_ALIGN - 1)
                         // ABI_STACK_ALIGN * ABI_STACK_ALIGN)

        frame_record_begin = area_size - frame_record_size

        return area_size, core_reg_begin, fp_reg_begin, frame_record_begin

    def _push_callee_save_regs_to_stack(self, mc):
        area_size, core_reg_begin, fp_reg_begin, frame_record_begin = \
                self._calculate_callee_save_area_size()

        # Subtract stack pointer
        mc.ADDI(r.sp.value, r.sp.value, -area_size)

        # Frame record
        mc.store_int(r.fp.value, r.sp.value, frame_record_begin)
        mc.store_int(r.ra.value, r.sp.value, frame_record_begin + XLEN)
        mc.ADDI(r.fp.value, r.sp.value, area_size)

        for i, reg in enumerate(r.callee_saved_registers_except_ra_sp_fp):
            mc.store_int(reg.value, r.sp.value, i * XLEN + core_reg_begin)
        for i, reg in enumerate(r.callee_saved_fp_registers):
            mc.store_float(reg.value, r.sp.value, i * FLEN + fp_reg_begin)

    def _pop_callee_save_regs_from_stack(self, mc):
        area_size, core_reg_begin, fp_reg_begin, frame_record_begin = \
                self._calculate_callee_save_area_size()
        for i, reg in enumerate(r.callee_saved_fp_registers):
            mc.load_float(reg.value, r.sp.value, i * FLEN + fp_reg_begin)
        for i, reg in enumerate(r.callee_saved_registers_except_ra_sp_fp):
            mc.load_int(reg.value, r.sp.value, i * XLEN + core_reg_begin)

        # Frame record
        mc.load_int(r.ra.value, r.sp.value, frame_record_begin + XLEN)
        mc.load_int(r.fp.value, r.sp.value, frame_record_begin)

        # Add (restore) stack pointer
        mc.ADDI(r.sp.value, r.sp.value, area_size)

    def _push_all_regs_to_jitframe(self, mc, ignored_regs, withfloats,
                                   callee_only=False):
        # Push general purpose registers
        base_ofs = self.cpu.get_baseofs_of_frame_field()
        if callee_only:
            regs = r.caller_saved_registers
        else:
            regs = r.registers_except_zero

        if not ignored_regs:
            for reg in regs:
                mc.store_int(reg.value, r.jfp.value,
                             base_ofs + reg.value * XLEN)
        else:
            for reg in ignored_regs:
                assert reg.is_core_reg()
            for reg in regs:
                if reg in ignored_regs:
                    continue
                mc.store_int(reg.value, r.jfp.value,
                             base_ofs + reg.value * XLEN)

        if withfloats:
            # Push floating point registers
            ofs = base_ofs + len(r.registers) * XLEN
            for reg in r.fp_registers:
                mc.store_float(reg.value, r.jfp.value, ofs + reg.value * FLEN)

    def _pop_all_regs_from_jitframe(self, mc, ignored_regs, withfloats,
                                    callee_only=False):
        # Pop general purpose registers
        base_ofs = self.cpu.get_baseofs_of_frame_field()
        if callee_only:
            regs = r.caller_saved_registers
        else:
            regs = r.registers_except_zero

        if not ignored_regs:
            for reg in regs:
                mc.load_int(reg.value, r.jfp.value,
                            base_ofs + reg.value * XLEN)
        else:
            for reg in ignored_regs:
                assert reg.is_core_reg()
            for reg in regs:
                if reg in ignored_regs:
                    continue
                mc.load_int(reg.value, r.jfp.value,
                            base_ofs + reg.value * XLEN)

        if withfloats:
            # Pop floating point registers
            ofs = base_ofs + len(r.registers) * XLEN
            for reg in r.fp_registers:
                mc.load_float(reg.value, r.jfp.value, ofs + reg.value * FLEN)

    def store_jf_descr(self, descrindex):
        scratch_reg = r.x31
        ofs = self.cpu.get_ofs_of_frame_field('jf_descr')
        self.load_from_gc_table(scratch_reg.value, descrindex)
        self.mc.store_int(scratch_reg.value, r.jfp.value, ofs)

    def push_gcmap(self, mc, gcmap, store=True):
        # Set gcmap address to jf_gcmap field.

        # rpython/jit/backend/llsupport/callbuilder.py passes a `store`
        # argument as keyword args. For RISC-V backend, we only support
        # `store=True` version.
        assert store

        scratch_reg = r.x31
        new_gcmap_adr = rffi.cast(lltype.Signed, gcmap)
        mc.load_int_imm(scratch_reg.value, new_gcmap_adr)

        ofs = self.cpu.get_ofs_of_frame_field('jf_gcmap')
        mc.store_int(scratch_reg.value, r.jfp.value, ofs)

    def pop_gcmap(self, mc):
        # Clear gcmap address from jf_gcmap field.
        ofs = self.cpu.get_ofs_of_frame_field('jf_gcmap')
        mc.store_int(r.x0.value, r.jfp.value, ofs)

    def generate_quick_failure(self, guardtok):
        startpos = self.mc.get_relative_pos()
        faildescrindex, target = self.store_info_on_descr(startpos, guardtok)

        self.store_jf_descr(faildescrindex)
        self.push_gcmap(self.mc, guardtok.gcmap)
        assert target
        self.mc.jal_abs(r.zero.value, target)
        return startpos

    def write_pending_failure_recoveries(self):
        for guardtok in self.pending_guards:
            guardtok.pos_recovery_stub = self.generate_quick_failure(guardtok)

    def process_pending_guards(self, rawstart):
        clt = self.current_clt
        for guardtok in self.pending_guards:
            descr = guardtok.faildescr
            assert isinstance(descr, AbstractFailDescr)

            failure_recovery_pos = rawstart + guardtok.pos_recovery_stub
            descr.adr_jump_offset = failure_recovery_pos
            relative_offset = guardtok.pos_recovery_stub - guardtok.offset
            guard_pos = rawstart + guardtok.offset

            if guardtok.guard_not_invalidated():
                clt.invalidate_positions.append((guard_pos, relative_offset))
            else:
                # Patch the guard jump to the stub
                assert check_simm21_arg(relative_offset)
                mc = InstrBuilder()
                mc.J(relative_offset)
                mc.copy_to_raw_memory(guard_pos)

    def fixup_target_tokens(self, rawstart):
        for targettoken in self.target_tokens_currently_compiling:
            targettoken._ll_loop_code += rawstart
        self.target_tokens_currently_compiling = None

    def reserve_gcref_table(self, allgcrefs):
        gcref_table_size = len(allgcrefs) * XLEN
        gcref_table_size = (gcref_table_size + 15) & ~15  # Align to 16

        # Reserve space at the beginning of the machine code for the gc table.
        # This lets us access gc table with pc-relative addressing.
        mc = self.mc
        assert mc.get_relative_pos() == 0
        for i in range(gcref_table_size):
            mc.writechar('\x00')

        self.setup_gcrefs_list(allgcrefs)

    def patch_gcref_table(self, looptoken, rawstart):
        self.gc_table_addr = rawstart
        tracer = self.cpu.gc_ll_descr.make_gcref_tracer(rawstart,
                                                        self._allgcrefs)
        gcreftracers = self.get_asmmemmgr_gcreftracers(looptoken)
        gcreftracers.append(tracer)  # Keepalive
        self.teardown_gcrefs_list()

    def load_from_gc_table(self, reg_num, index):
        address_in_buffer = index * XLEN  # at the start of the buffer
        p_location = self.mc.get_relative_pos(break_basic_block=False)
        offset = address_in_buffer - p_location
        self.mc.load_int_pc_rel(reg_num, offset)

    def setup(self, looptoken):
        OpAssembler.setup(self, looptoken)
        assert self.memcpy_addr != 0, 'setup_once() not called?'

        self.current_clt = looptoken.compiled_loop_token
        self.mc = InstrBuilder()
        self.pending_guards = []
        self.target_tokens_currently_compiling = {}

        allblocks = self.get_asmmemmgr_blocks(looptoken)
        self.datablockwrapper = MachineDataBlockWrapper(self.cpu.asmmemmgr,
                                                        allblocks)
        self.mc.datablockwrapper = self.datablockwrapper

        self._finish_gcmap = jitframe.NULLGCMAP

    def teardown(self):
        self.current_clt = None
        self._regalloc = None
        self.mc = None
        self.pending_guards = None

    def materialize_loop(self, looptoken):
        # Finalizes data block
        self.datablockwrapper.done()
        self.datablockwrapper = None

        # Finalizes instruction builder, combines the code buffers, and copy
        # them to an executable memory region.
        allblocks = self.get_asmmemmgr_blocks(looptoken)
        size = self.mc.get_relative_pos()
        rawstart = self.mc.materialize(self.cpu, allblocks,
                                       self.cpu.gc_ll_descr.gcrootmap)
        return rawstart

    def _build_failure_recovery(self, exc, withfloats=False):
        mc = InstrBuilder()
        self._push_all_regs_to_jitframe(mc, [], withfloats)

        if exc:
            # Move the exception from `self.cpu.pos_exc_value()` to JITFrame
            # `jf_guard_exc` and then reset the data in
            # `self.cpu.pos_exc_value()` and `self.cpu.pos_exception()`.

            scratch_reg = r.x31
            scratch2_reg = r.x10  # Will be set by `_call_footer` soon.

            # Load exc_value from `self.cpu.pos_exc_value()`.
            mc.load_int_imm(scratch_reg.value, self.cpu.pos_exc_value())
            mc.load_int(scratch2_reg.value, scratch_reg.value, 0)

            # Clear `self.cpu.pos_exc_value()`.
            mc.store_int(r.x0.value, scratch_reg.value, 0)

            # Store exc_value to `jf_guard_exc`.
            ofs = self.cpu.get_ofs_of_frame_field('jf_guard_exc')
            mc.store_int(scratch2_reg.value, r.jfp.value, ofs)

            # Clear `self.cpu.pos_exception()`.
            mc.load_int_imm(scratch_reg.value, self.cpu.pos_exception())
            mc.store_int(r.x0.value, scratch_reg.value, 0)

        self._call_footer(mc)

        rawstart = mc.materialize(self.cpu, [])
        self.failure_recovery_code[exc + 2 * withfloats] = rawstart

    def _build_wb_slowpath(self, withcards, withfloats=False, for_frame=False):
        # Build a slow path to call GC write barrier.
        #
        # This builds a helper function called from the fast path of write
        # barriers.  It must save all registers, and optionally all fp
        # registers.  It takes a single argument which is in `r.x10`.  It must
        # keep stack alignment accordingly.

        descr = self.cpu.gc_ll_descr.write_barrier_descr
        if descr is None:
            return

        if not withcards:
            func = descr.get_write_barrier_fn(self.cpu)
        else:
            if descr.jit_wb_cards_set == 0:
                return
            func = descr.get_write_barrier_from_array_fn(self.cpu)
            if func == 0:
                return

        mc = InstrBuilder()

        # Allocate two callee-save scratch registers to handle exception
        # save and restore.
        exc0 = r.x24
        exc1 = r.x25
        fp_align_size = 0
        stack_size = 0
        core_regs_to_be_spilled = []

        if not for_frame:
            self._push_all_regs_to_jitframe(mc, [], withfloats,
                                            callee_only=True)
        else:
            # NOTE: Don't save registers to the jitframe here!  It might
            # override already-saved values that will be restored later.
            #
            # we're possibly called from the slowpath of malloc.  save the
            # caller saved registers assuming GC does not collect here.

            core_regs_to_be_spilled = r.caller_saved_registers + [exc0, exc1]

            core_reg_size = len(core_regs_to_be_spilled) * XLEN
            core_reg_size_aligned = (core_reg_size + FLEN - 1) // FLEN * FLEN
            fp_align_size = core_reg_size_aligned - core_reg_size
            fp_reg_size = len(r.caller_saved_fp_registers) * FLEN
            stack_size = (core_reg_size_aligned + fp_reg_size +
                          ABI_STACK_ALIGN - 1) \
                    // ABI_STACK_ALIGN * ABI_STACK_ALIGN

            mc.ADDI(r.sp.value, r.sp.value, -stack_size)

            # Spill caller-saved registers.
            cur_stack = 0
            for reg in core_regs_to_be_spilled:
                mc.store_int(reg.value, r.sp.value, cur_stack)
                cur_stack += XLEN

            # Spill caller-saved float registers.
            cur_stack += fp_align_size
            for reg in r.caller_saved_fp_registers:
                mc.store_float(reg.value, r.sp.value, cur_stack)
                cur_stack += FLEN

            self._store_and_reset_exception(mc, exc0, exc1)

        func = rffi.cast(lltype.Signed, func)
        mc.load_int_imm(r.ra.value, func)
        mc.JALR(r.ra.value, r.ra.value, 0)

        if not for_frame:
            self._pop_all_regs_from_jitframe(mc, [], withfloats,
                                             callee_only=True)
        else:
            self._restore_exception(mc, exc0, exc1)

            # Restore caller-saved registers.
            cur_stack = 0
            for reg in core_regs_to_be_spilled:
                mc.load_int(reg.value, r.sp.value, cur_stack)
                cur_stack += XLEN

            # Restore caller-saved float registers.
            cur_stack += fp_align_size
            for reg in r.caller_saved_fp_registers:
                mc.load_float(reg.value, r.sp.value, cur_stack)
                cur_stack += FLEN

            mc.ADDI(r.sp.value, r.sp.value, stack_size)

        if withcards:
            # Load and mask the `jit_wb_cards_set_singlebyte` to `x31`, so that
            # the caller of the `wb_slowpath` can emit a simple
            # `BEQZ x31, end_update_card_table`.  This helps us save 2
            # instructions per `COND_CALL_GC_WB_ARRAY`.
            mc.LBU(r.x31.value, r.x10.value, descr.jit_wb_if_flag_byteofs)
            mc.ANDI(r.x31.value, r.x31.value, 0x80)

        mc.RET()

        rawstart = mc.materialize(self.cpu, [])
        if for_frame:
            self.wb_slowpath[4] = rawstart
        else:
            self.wb_slowpath[withcards + 2 * withfloats] = rawstart

    def build_frame_realloc_slowpath(self):
        pass

    def update_frame_depth(self, frame_depth):
        baseofs = self.cpu.get_baseofs_of_frame_field()
        self.current_clt.frame_info.update_frame_depth(baseofs, frame_depth)

    def _store_and_reset_exception(self, mc, exc_val_loc=None,
                                   exc_tp_loc=None, on_frame=False):
        # Move the exception object and type from the addresses provided by
        # `self.cpu.pos_exc_value()` and `self.cpu.pos_excption()` to (1) the
        # specified registers and/or (2) `JITFrame.jf_guard_exc` and then
        # reset the data at the addresses provided by `self.cpu.pos_*()`.

        scratch_reg = r.x31
        assert exc_val_loc is not scratch_reg
        assert exc_tp_loc is not scratch_reg

        # Move the data at `self.cpu.pos_exc_value()` to specified location.
        mc.load_int_imm(scratch_reg.value, self.cpu.pos_exc_value())
        if exc_val_loc is not None:
            assert exc_val_loc.is_core_reg()
            mc.load_int(exc_val_loc.value, scratch_reg.value, 0)
        if on_frame:
            # Store exc_value to the JITFRAME.jf_guard_exc
            scratch2_reg = r.ra  # Clobber r.ra is fine when `on_frame=True`.
            ofs = self.cpu.get_ofs_of_frame_field('jf_guard_exc')
            mc.load_int(scratch2_reg.value, scratch_reg.value, 0)
            mc.store_int(scratch2_reg.value, r.jfp.value, ofs)

        # Reset `self.cpu.pos_exc_value()`.
        mc.store_int(r.x0.value, scratch_reg.value, 0)

        # Move the data at `self.cpu.pos_exception()` to specified location.
        mc.load_int_imm(scratch_reg.value, self.cpu.pos_exception())
        if exc_tp_loc is not None:
            assert exc_tp_loc.is_core_reg()
            mc.load_int(exc_tp_loc.value, scratch_reg.value, 0)

        # Reset `self.cpu.pos_exception()`.
        mc.store_int(r.x0.value, scratch_reg.value, 0)

    def _restore_exception(self, mc, exc_val_loc, exc_tp_loc):
        # Restore `self.cpu.pos_exc_value()` and `self.cpu.pos_exception()`
        # from `exc_val_loc` (or `jf_guard_exc`) and `exc_tp_loc` registers.

        # Allocate scratch registeres.
        scratch_reg = r.x31
        scratch2_reg = r.ra
        assert (exc_val_loc is not scratch_reg and
                exc_val_loc is not scratch2_reg)
        assert (exc_tp_loc is not scratch_reg and
                exc_tp_loc is not scratch2_reg)

        # Restore `pos_exc_value`.
        mc.load_int_imm(scratch_reg.value, self.cpu.pos_exc_value())
        if exc_val_loc is not None:
            mc.store_int(exc_val_loc.value, scratch_reg.value, 0)
        else:
            # Load `exc_value` from JITFRAME and put it in `pos_exc_value`.
            ofs = self.cpu.get_ofs_of_frame_field('jf_guard_exc')
            mc.load_int(scratch2_reg.value, r.jfp.value, ofs)
            mc.store_int(scratch2_reg.value, scratch_reg.value, 0)

            # Reset `jf_guard_exc` in the JITFRAME.
            mc.store_int(r.x0.value, r.jfp.value, ofs)

        # Restore `pos_exception` from `exc_tp_loc`.
        mc.load_int_imm(scratch_reg.value, self.cpu.pos_exception())
        mc.store_int(exc_tp_loc.value, scratch_reg.value, 0)

    def _build_propagate_exception_path(self):
        pass

    def _build_cond_call_slowpath(self, supports_floats, callee_only):
        """ This builds a general call slowpath, for whatever call happens to
        come.

        The address of the callee function comes in r.x30.
        The returning value is stored in r.x30.
        """
        mc = InstrBuilder()

        # Spill registers to JITFRAME
        #
        # Ignore jfp for _reload_frame_if_necessary, x30 for return, x31 for
        # scratch.
        ignore_regs_for_push_pop = [r.jfp, r.x30, r.x31]
        self._push_all_regs_to_jitframe(mc, ignore_regs_for_push_pop,
                                        supports_floats,
                                        callee_only)  # Spills r.ra

        # Branch to the callee function.
        mc.JALR(r.ra.value, r.x30.value, 0)

        # Move return value to r.x30.
        mc.MV(r.x30.value, r.x10.value)

        # Restore registers from JITFRAME
        tmplocs = [r.x29]  # Use callee-saved register as scratch regs
        self._reload_frame_if_necessary(mc, tmplocs)
        self._pop_all_regs_from_jitframe(mc, ignore_regs_for_push_pop,
                                         supports_floats,
                                         callee_only)  # Restores r.ra
        mc.RET()
        return mc.materialize(self.cpu, [])

    def _reload_frame_if_necessary(self, mc, tmplocs):
        gcrootmap = self.cpu.gc_ll_descr.gcrootmap
        if gcrootmap and gcrootmap.is_shadow_stack:
            stack_top_ptr_addr = gcrootmap.get_root_stack_top_addr()
            mc.load_int_imm(r.jfp.value, stack_top_ptr_addr)
            mc.load_int(r.jfp.value, r.jfp.value, 0)
            mc.load_int(r.jfp.value, r.jfp.value, -XLEN)
        wbdescr = self.cpu.gc_ll_descr.write_barrier_descr
        if gcrootmap and wbdescr:
            # Frame never uses card marking, so we enforce this is not an
            # array.
            self._write_barrier_fastpath(mc, wbdescr, [r.jfp], tmplocs,
                                         array=False, is_frame=True)

    def _build_stack_check_slowpath(self):
        pass

    def _load_fp_imm(self, loc, imm):
        """Load a float immediate value to a fp register."""
        # TODO: Switch to pc-relative addressing
        self.mc.load_int_imm(r.x31.value, imm.get_addr())
        self.mc.load_float(loc.value, r.x31.value, 0)

    def load_imm(self, loc, imm):
        """Load an immediate value into a register"""
        if loc.is_core_reg():
            assert imm.is_imm()
            self.mc.load_int_imm(loc.value, imm.value)
        else:
            assert loc.is_fp_reg() and imm.is_imm_float()
            self._load_fp_imm(loc, imm)

    def regalloc_mov(self, prev_loc, loc):
        """Moves a value from a previous location to some other location"""
        if prev_loc.is_imm():
            return self._mov_imm_to_loc(prev_loc, loc)
        elif prev_loc.is_stack():
            self._mov_stack_to_loc(prev_loc, loc)
        elif prev_loc.is_core_reg():
            self._mov_reg_to_loc(prev_loc, loc)
        elif prev_loc.is_fp_reg():
            self._mov_fp_reg_to_loc(prev_loc, loc)
        elif prev_loc.is_imm_float():
            self._mov_imm_float_to_loc(prev_loc, loc)
        else:
            assert 0, 'unsupported case'
    mov_loc_loc = regalloc_mov

    def _mov_imm_to_loc(self, prev_loc, loc):
        if loc.is_core_reg():
            self.mc.load_int_imm(loc.value, prev_loc.value)
        else:
            assert 0, 'unsupported case'

    def _mov_stack_to_loc(self, prev_loc, loc):
        offset = prev_loc.value
        if loc.is_core_reg():
            self.mc.load_int(loc.value, r.jfp.value, offset)
        elif loc.is_fp_reg():
            self.mc.load_float(loc.value, r.jfp.value, offset)
        else:
            assert 0, 'unsupported case'

    def _mov_reg_to_loc(self, prev_loc, loc):
        if loc.is_core_reg():
            self.mc.MV(loc.value, prev_loc.value)
        elif loc.is_stack():
            self.mc.store_int(prev_loc.value, r.jfp.value, loc.value)
        else:
            assert 0, 'unsupported case'

    def _mov_fp_reg_to_loc(self, prev_loc, loc):
        if loc.is_fp_reg():
            self.mc.FMV_D(loc.value, prev_loc.value)
        elif loc.is_core_reg():
            assert XLEN == 8 and FLEN == 8
            self.mc.FMV_X_D(loc.value, prev_loc.value)
        elif loc.is_stack():
            self.mc.store_float(prev_loc.value, r.jfp.value, loc.value)
        else:
            assert 0, 'unsupported case'

    def _mov_imm_float_to_loc(self, prev_loc, loc):
        if loc.is_fp_reg():
            self._load_fp_imm(loc, prev_loc)
        elif loc.is_stack():
            self._load_fp_imm(r.f31, prev_loc)
            self.mc.store_float(r.f31.value, r.jfp.value, loc.value)
        else:
            assert 0, 'unsupported case'

    def mov_loc_to_raw_stack(self, loc, sp_offset):
        # Move a value to sp[sp_offset], which is usually for foreign function
        # calls.
        if loc.is_core_reg():
            self.mc.store_int(loc.value, r.sp.value, sp_offset)
        elif loc.is_stack():
            # Move a value from JITFRAME stack to raw stack.
            scratch_reg = r.x31
            self.mc.load_int(scratch_reg.value, r.jfp.value, loc.value)
            self.mc.store_int(scratch_reg.value, r.sp.value, sp_offset)
        elif loc.is_fp_reg():
            self.mc.store_float(loc.value, r.sp.value, sp_offset)
        elif loc.is_imm():
            scratch_reg = r.x31
            self.mc.load_int_imm(scratch_reg.value, loc.value)
            self.mc.store_int(scratch_reg.value, r.sp.value, sp_offset)
        else:
            assert 0, 'unsupported case'

    def regalloc_push(self, loc, already_pushed):
        """Push the value stored in `loc` to the stack top.

        Side effect: r.x31 or r.f31 may be overwritten."""

        offset = SCRATCH_STACK_SLOT_SIZE * (~already_pushed)

        if loc.type == FLOAT:
            if not loc.is_fp_reg():
                self.regalloc_mov(loc, r.f31)
                loc = r.f31
            self.mc.store_float(loc.value, r.sp.value, offset)
        else:
            if not loc.is_core_reg():
                self.regalloc_mov(loc, r.x31)
                loc = r.x31
            self.mc.store_int(loc.value, r.sp.value, offset)

    def regalloc_pop(self, loc, already_pushed):
        """Pop the value from the top of the stack to `loc`.

        Side effect: r.x31 or r.f31 may be overwritten."""

        offset = SCRATCH_STACK_SLOT_SIZE * (~already_pushed)

        if loc.type == FLOAT:
            if loc.is_fp_reg():
                self.mc.load_float(loc.value, r.sp.value, offset)
            else:
                self.mc.load_float(r.f31.value, r.sp.value, offset)
                self.regalloc_mov(r.f31, loc)
        else:
            if loc.is_core_reg():
                self.mc.load_int(loc.value, r.sp.value, offset)
            else:
                self.mc.load_int(r.x31.value, r.sp.value, offset)
                self.regalloc_mov(r.x31, loc)

    def regalloc_prepare_move(self, src, dst, tmp):
        """Move `src` to `tmp` and return `tmp` if `src`-to-`dst` is a
        stack-to-stack or imm-to-stack move."""
        if dst.is_stack() and (src.is_stack() or src.is_imm()):
            self.regalloc_mov(src, tmp)
            return tmp
        return src
