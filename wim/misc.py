# vim: sw=4 ts=4 sts=4 expandtab
#
# Copyright (C) 2013 Intel Corporation.
# Copyright (C) 2025 Advanced Micro Devices, Inc.
#
# SPDX-License-Identifier: GPL-2.0-only
#
# DESCRIPTION
# This module provides a place to collect various wim-related utils
# for the OpenEmbedded Image Tools.
#
# AUTHORS
# Tom Zanussi <tom.zanussi (at] linux.intel.com>
# Trevor Woerner <trevor.woerner (at] amd.com>
#
"""Miscellaneous functions."""

import logging
import os
import re
import subprocess
import shutil

from collections import defaultdict

from wim import WimError

logger = logging.getLogger('wim')

# executable -> recipe pairs for exec_native_cmd
NATIVE_RECIPES = {"bmaptool": "bmaptool",
                  "dumpe2fs": "e2fsprogs",
                  "grub-mkimage": "grub-efi",
                  "isohybrid": "syslinux",
                  "mcopy": "mtools",
                  "mdel" : "mtools",
                  "mdeltree" : "mtools",
                  "mdir" : "mtools",
                  "mkdosfs": "dosfstools",
                  "mkisofs": "cdrtools",
                  "mkfs.btrfs": "btrfs-tools",
                  "mkfs.erofs": "erofs-utils",
                  "mkfs.ext2": "e2fsprogs",
                  "mkfs.ext3": "e2fsprogs",
                  "mkfs.ext4": "e2fsprogs",
                  "mkfs.vfat": "dosfstools",
                  "mksquashfs": "squashfs-tools",
                  "mkswap": "util-linux",
                  "mmd": "mtools",
                  "parted": "parted",
                  "sfdisk": "util-linux",
                  "sgdisk": "gptfdisk",
                  "syslinux": "syslinux",
                  "tar": "tar"
                 }

def runtool(cmdln_or_args):
    """ wrapper for most of the subprocess calls
    input:
        cmdln_or_args: can be both args and cmdln str (shell=True)
    return:
        rc, output
    """
    if isinstance(cmdln_or_args, list):
        cmd = cmdln_or_args[0]
        shell = False
    else:
        import shlex
        cmd = shlex.split(cmdln_or_args)[0]
        shell = True

    sout = subprocess.PIPE
    serr = subprocess.STDOUT

    try:
        process = subprocess.Popen(cmdln_or_args, stdout=sout,
                                   stderr=serr, shell=shell)
        sout, serr = process.communicate()
        # combine stdout and stderr, filter None out and decode
        out = ''.join([out.decode('utf-8') for out in [sout, serr] if out])
    except OSError as err:
        if err.errno == 2:
            # [Errno 2] No such file or directory
            raise WimError('Cannot run command: %s, lost dependency?' % cmd)
        else:
            raise # relay

    return process.returncode, out

def _exec_cmd(cmd_and_args, as_shell=False):
    """
    Execute command, catching stderr, stdout

    Need to execute as_shell if the command uses wildcards
    """
    logger.debug("_exec_cmd: %s", cmd_and_args)
    args = cmd_and_args.split()
    logger.debug(args)

    if as_shell:
        ret, out = runtool(cmd_and_args)
    else:
        ret, out = runtool(args)
    out = out.strip()
    if ret != 0:
        raise WimError("_exec_cmd: %s returned '%s' instead of 0\noutput: %s" % \
                       (cmd_and_args, ret, out))

    logger.debug("_exec_cmd: output for %s (rc = %d): %s",
                 cmd_and_args, ret, out)

    return ret, out


def exec_cmd(cmd_and_args, as_shell=False):
    """
    Execute command, return output
    """
    return _exec_cmd(cmd_and_args, as_shell)[1]

def find_executable(cmd, paths):
    recipe = cmd
    if recipe in NATIVE_RECIPES:
        recipe =  NATIVE_RECIPES[recipe]
    provided = get_bitbake_var("ASSUME_PROVIDED")
    if provided and "%s-native" % recipe in provided:
        return True

    return shutil.which(cmd, path=paths)

def exec_native_cmd(cmd_and_args, native_sysroot, pseudo=""):
    """
    Execute native command, catching stderr, stdout

    Need to execute as_shell if the command uses wildcards

    Always need to execute native commands as_shell
    """
    # The reason -1 is used is because there may be "export" commands.
    args = cmd_and_args.split(';')[-1].split()
    logger.debug(args)

    if pseudo:
        cmd_and_args = pseudo + cmd_and_args

    hosttools_dir = get_bitbake_var("HOSTTOOLS_DIR")
    target_sys = get_bitbake_var("TARGET_SYS")

    native_paths = "%s/sbin:%s/usr/sbin:%s/usr/bin:%s/usr/bin/%s:%s/bin:%s" % \
                   (native_sysroot, native_sysroot,
                    native_sysroot, native_sysroot, target_sys,
                    native_sysroot, hosttools_dir)

    native_cmd_and_args = "export PATH=%s:$PATH;%s" % \
                   (native_paths, cmd_and_args)
    logger.debug("exec_native_cmd: %s", native_cmd_and_args)

    # If the command isn't in the native sysroot say we failed.
    if find_executable(args[0], native_paths):
        ret, out = _exec_cmd(native_cmd_and_args, True)
    else:
        ret = 127
        out = "can't find native executable %s in %s" % (args[0], native_paths)

    prog = args[0]
    # shell command-not-found
    if ret == 127 \
       or (pseudo and ret == 1 and out == "Can't find '%s' in $PATH." % prog):
        msg = "A native program %s required to build the image "\
              "was not found (see details above).\n\n" % prog
        recipe = NATIVE_RECIPES.get(prog)
        if recipe:
            msg += "Please make sure the SDK has %s-native in its nativesdk.\n" % recipe
        else:
            msg += "Wim failed to find a recipe to build native %s. Please "\
                   "file a bug against wim.\n" % prog
        raise WimError(msg)

    return ret, out
