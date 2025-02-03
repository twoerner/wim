#!/usr/bin/env python3
#vim: sw=4 ts=4 sts=4 expandtab
#
# Copyright (C) 2013 Intel Corporation.
# Copyright (C) 2025 Advanced Micro Devices, Inc.
#
# SPDX-License-Identifier: GPL-2.0-only
#
# DESCRIPTION 'wim' is a wic Image Modifier that users can
# use to modify bootable images.  Invoking it without any arguments
# will display help screens for the 'wim' command and list the
# available 'wim' subcommands.  Invoking a subcommand without any
# arguments will likewise display help screens for the specified
# subcommand.  Please use that interface for detailed help.
#
# AUTHORS
# Tom Zanussi <tom.zanussi (at] linux.intel.com>
# Trevor Woerner <trevor.woerner (at] amd.com>
#
__version__ = "0.1.0"

# Python Standard Library modules
import os
import sys
import argparse
import logging
import subprocess
import shutil

from collections import namedtuple

from . import WimError
from . import engine
from . import help as hlp


def wim_logger():
    """Create and convfigure wim logger."""
    logger = logging.getLogger('wim')
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler()

    formatter = logging.Formatter('%(levelname)s: %(message)s')
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    return logger

logger = wim_logger()

def wim_ls_subcommand(args, usage_str):
    """
    Command-line handling for list content of images.
    The real work is done by engine.wim_ls()
    """
    engine.wim_ls(args, args.native_sysroot)

def wim_cp_subcommand(args, usage_str):
    """
    Command-line handling for copying files/dirs to images.
    The real work is done by engine.wim_cp()
    """
    engine.wim_cp(args, args.native_sysroot)

def wim_rm_subcommand(args, usage_str):
    """
    Command-line handling for removing files/dirs from images.
    The real work is done by engine.wim_rm()
    """
    engine.wim_rm(args, args.native_sysroot)

def wim_write_subcommand(args, usage_str):
    """
    Command-line handling for writing images.
    The real work is done by engine.wim_write()
    """
    engine.wim_write(args, args.native_sysroot)

def wim_help_subcommand(args, usage_str):
    """
    Command-line handling for help subcommand to keep the current
    structure of the function definitions.
    """
    pass


def wim_help_topic_subcommand(usage_str, help_str):
    """
    Display function for help 'sub-subcommands'.
    """
    print(help_str)
    return


wim_help_topic_usage = """
"""

helptopics = {
    "ls":        [wim_help_topic_subcommand,
                  wim_help_topic_usage,
                  hlp.wim_ls_help],
    "cp":        [wim_help_topic_subcommand,
                  wim_help_topic_usage,
                  hlp.wim_cp_help],
    "rm":        [wim_help_topic_subcommand,
                  wim_help_topic_usage,
                  hlp.wim_rm_help],
    "write":     [wim_help_topic_subcommand,
                  wim_help_topic_usage,
                  hlp.wim_write_help],
}

def wim_init_parser_create(subparser):
    subparser.add_argument("-n", "--native-sysroot", dest="native_sysroot",
                      help="path to the native sysroot containing the tools to use")
    subparser.add_argument("-m", "--bmap", action="store_true", help="generate .bmap")
    subparser.add_argument("-D", "--debug", dest="debug", action="store_true",
                      default=False, help="output debug information")
    return

def wim_init_parser_list(subparser):
    subparser.add_argument("help_for", default=[], nargs='*')
    return

def imgtype(arg):
    """
    Custom type for ArgumentParser
    Converts path spec to named tuple: (image, partition, path)
    """
    image = arg
    part = path = None
    if ':' in image:
        image, part = image.split(':')
        if '/' in part:
            part, path = part.split('/', 1)
        if not path:
            path = '/'

    if not os.path.isfile(image):
        err = "%s is not a regular file or symlink" % image
        raise argparse.ArgumentTypeError(err)

    return namedtuple('ImgType', 'image part path')(image, part, path)

def wim_init_parser_ls(subparser):
    subparser.add_argument("path", type=imgtype,
                        help="image spec: <image>[:<vfat partition>[<path>]]")
    subparser.add_argument("-n", "--native-sysroot",
                        help="path to the native sysroot containing the tools")

def imgpathtype(arg):
    img = imgtype(arg)
    if img.part is None:
        raise argparse.ArgumentTypeError("partition number is not specified")
    return img

def wim_init_parser_cp(subparser):
    subparser.add_argument("src",
                        help="image spec: <image>:<vfat partition>[<path>] or <file>")
    subparser.add_argument("dest",
                        help="image spec: <image>:<vfat partition>[<path>] or <file>")
    subparser.add_argument("-n", "--native-sysroot",
                        help="path to the native sysroot containing the tools")

def wim_init_parser_rm(subparser):
    subparser.add_argument("path", type=imgpathtype,
                        help="path: <image>:<vfat partition><path>")
    subparser.add_argument("-n", "--native-sysroot",
                        help="path to the native sysroot containing the tools")
    subparser.add_argument("-r", dest="recursive_delete", action="store_true", default=False,
                        help="remove directories and their contents recursively, "
                        " this only applies to ext* partition")

def expandtype(rules):
    """
    Custom type for ArgumentParser
    Converts expand rules to the dictionary {<partition>: size}
    """
    if rules == 'auto':
        return {}
    result = {}
    for rule in rules.split(','):
        try:
            part, size = rule.split(':')
        except ValueError:
            raise argparse.ArgumentTypeError("Incorrect rule format: %s" % rule)

        if not part.isdigit():
            raise argparse.ArgumentTypeError("Rule '%s': partition number must be integer" % rule)

        # validate size
        multiplier = 1
        for suffix, mult in [('K', 1024), ('M', 1024 * 1024), ('G', 1024 * 1024 * 1024)]:
            if size.upper().endswith(suffix):
                multiplier = mult
                size = size[:-1]
                break
        if not size.isdigit():
            raise argparse.ArgumentTypeError("Rule '%s': size must be integer" % rule)

        result[int(part)] = int(size) * multiplier

    return result

def wim_init_parser_write(subparser):
    subparser.add_argument("image",
                        help="path to the wic image")
    subparser.add_argument("target",
                        help="target file or device")
    subparser.add_argument("-e", "--expand", type=expandtype,
                        help="expand rules: auto or <partition>:<size>[,<partition>:<size>]")
    subparser.add_argument("-n", "--native-sysroot",
                        help="path to the native sysroot containing the tools")

def wim_init_parser_help(subparser):
    helpparsers = subparser.add_subparsers(dest='help_topic', help=hlp.wim_usage)
    for helptopic in helptopics:
        helpparsers.add_parser(helptopic, help=helptopics[helptopic][2])
    return


subcommands = {
    "ls":        [wim_ls_subcommand,
                  hlp.wim_ls_usage,
                  hlp.wim_ls_help,
                  wim_init_parser_ls],
    "cp":        [wim_cp_subcommand,
                  hlp.wim_cp_usage,
                  hlp.wim_cp_help,
                  wim_init_parser_cp],
    "rm":        [wim_rm_subcommand,
                  hlp.wim_rm_usage,
                  hlp.wim_rm_help,
                  wim_init_parser_rm],
    "write":     [wim_write_subcommand,
                  hlp.wim_write_usage,
                  hlp.wim_write_help,
                  wim_init_parser_write],
    "help":      [wim_help_subcommand,
                  wim_help_topic_usage,
                  hlp.wim_help_help,
                  wim_init_parser_help]
}


def init_parser(parser):
    parser.add_argument("--version", action="version",
        version="%(prog)s {version}".format(version=__version__))
    parser.add_argument("-D", "--debug", dest="debug", action="store_true",
        default=False, help="output debug information")

    subparsers = parser.add_subparsers(dest='command', help=hlp.wim_usage)
    for subcmd in subcommands:
        subparser = subparsers.add_parser(subcmd, help=subcommands[subcmd][2])
        subcommands[subcmd][3](subparser)

class WimArgumentParser(argparse.ArgumentParser):
     def format_help(self):
         return hlp.wim_help

def main():
    parser = WimArgumentParser(
        description="wim version %s" % __version__)

    init_parser(parser)

    args = parser.parse_args(sys.argv[1:])

    if args.debug:
        logger.setLevel(logging.DEBUG)

    if "command" in vars(args):
        if args.command == "help":
            if args.help_topic is None:
                parser.print_help()
            elif args.help_topic in helptopics:
                hlpt = helptopics[args.help_topic]
                hlpt[0](hlpt[1], hlpt[2])
            return 0

    # validate wim cp src and dest parameter to identify which one of it is
    # image and cast it into imgtype
    if args.command == "cp":
        if ":" in args.dest:
            args.dest = imgtype(args.dest)
        elif ":" in args.src:
            args.src = imgtype(args.src)
        else:
            raise argparse.ArgumentTypeError("no image or partition number specified.")

    return hlp.invoke_subcommand(args, parser, hlp.wim_help_usage, subcommands)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WimError as err:
        print()
        logger.error(err)
        sys.exit(1)
