# vim: sw=4 ts=4 sts=4 expandtab
#
# Copyright (C) 2013 Intel Corporation.
# Copyright (C) 2025 Advanced Micro Devices, Inc.
#
# SPDX-License-Identifier: GPL-2.0-only
#
# DESCRIPTION

# This module implements the image manipulation engine used by 'wim' to
# modify images.
#
# AUTHORS
# Tom Zanussi <tom.zanussi (at] linux.intel.com>
# Trevor Woerner <trevor.woerner (at] amd.com>
#

import logging
import os
import tempfile
import json
import subprocess
import shutil
import re

from collections import namedtuple, OrderedDict

from wim import WimError
from wim.filemap import sparse_copy
from wim.misc import exec_cmd

logger = logging.getLogger('wim')

class Disk:
    def __init__(self, imagepath, native_sysroot, fstypes=('fat', 'ext')):
        self.imagepath = imagepath
        self.native_sysroot = native_sysroot
        self.fstypes = fstypes
        self._partitions = None
        self._partimages = {}
        self._lsector_size = None
        self._psector_size = None
        self._ptable_format = None

        # define sector size
        self.sector_size = None

        # find parted
        # read paths from $PATH environment variable
        # if it fails, use hardcoded paths
        pathlist = "/bin:/usr/bin:/usr/sbin:/sbin/"
        try:
            self.paths = os.environ['PATH'] + ":" + pathlist
        except KeyError:
            self.paths = pathlist

        if native_sysroot:
            for path in pathlist.split(':'):
                self.paths = "%s%s:%s" % (native_sysroot, path, self.paths)

        self.parted = shutil.which("parted", path=self.paths)
        if not self.parted:
            raise WimError("Can't find executable parted")

        self.partitions = self.get_partitions()

    def __del__(self):
        for path in self._partimages.values():
            os.unlink(path)

    def get_partitions(self):
        if self._partitions is None:
            self._partitions = OrderedDict()

            if self.sector_size is not None:
                out = exec_cmd("export PARTED_SECTOR_SIZE=%d; %s -sm %s unit B print" % \
                           (self.sector_size, self.parted, self.imagepath), True)
            else:
                out = exec_cmd("%s -sm %s unit B print" % (self.parted, self.imagepath))

            parttype = namedtuple("Part", "pnum start end size fstype")
            splitted = out.splitlines()
            # skip over possible errors in exec_cmd output
            try:
                idx =splitted.index("BYT;")
            except ValueError:
                raise WimError("Error getting partition information from %s" % (self.parted))
            lsector_size, psector_size, self._ptable_format = splitted[idx + 1].split(":")[3:6]
            self._lsector_size = int(lsector_size)
            self._psector_size = int(psector_size)
            for line in splitted[idx + 2:]:
                pnum, start, end, size, fstype = line.split(':')[:5]
                partition = parttype(int(pnum), int(start[:-1]), int(end[:-1]),
                                     int(size[:-1]), fstype)
                self._partitions[pnum] = partition

        return self._partitions

    def __getattr__(self, name):
        """Get path to the executable in a lazy way."""
        if name in ("mdir", "mcopy", "mdel", "mdeltree", "sfdisk", "e2fsck",
                    "resize2fs", "mkswap", "mkdosfs", "debugfs","blkid"):
            aname = "_%s" % name
            if aname not in self.__dict__:
                setattr(self, aname, shutil.which(name, path=self.paths))
                if aname not in self.__dict__ or self.__dict__[aname] is None:
                    raise WimError("Can't find executable '{}'".format(name))
            return self.__dict__[aname]
        return self.__dict__[name]

    def _get_part_image(self, pnum):
        if pnum not in self.partitions:
            raise WimError("Partition %s is not in the image" % pnum)
        part = self.partitions[pnum]
        # check if fstype is supported
        for fstype in self.fstypes:
            if part.fstype.startswith(fstype):
                break
        else:
            raise WimError("Not supported fstype: {}".format(part.fstype))
        if pnum not in self._partimages:
            tmpf = tempfile.NamedTemporaryFile(prefix="wim-part")
            dst_fname = tmpf.name
            tmpf.close()
            sparse_copy(self.imagepath, dst_fname, skip=part.start, length=part.size)
            self._partimages[pnum] = dst_fname

        return self._partimages[pnum]

    def _put_part_image(self, pnum):
        """Put partition image into partitioned image."""
        sparse_copy(self._partimages[pnum], self.imagepath,
                    seek=self.partitions[pnum].start)

    def dir(self, pnum, path):
        if pnum not in self.partitions:
            raise WimError("Partition %s is not in the image" % pnum)

        if self.partitions[pnum].fstype.startswith('ext'):
            return exec_cmd("{} {} -R 'ls -l {}'".format(self.debugfs,
                                                         self._get_part_image(pnum),
                                                         path), as_shell=True)
        else: # fat
            return exec_cmd("{} -i {} ::{}".format(self.mdir,
                                                   self._get_part_image(pnum),
                                                   path))

    def copy(self, src, dest):
        """Copy partition image into wim image."""
        pnum =  dest.part if isinstance(src, str) else src.part

        if self.partitions[pnum].fstype.startswith('ext'):
            if isinstance(src, str):
                cmd = "printf 'cd {}\nwrite {} {}\n' | {} -w {}".\
                      format(os.path.dirname(dest.path), src, os.path.basename(src),
                             self.debugfs, self._get_part_image(pnum))
            else: # copy from wim
                # run both dump and rdump to support both files and directory
                cmd = "printf 'cd {}\ndump /{} {}\nrdump /{} {}\n' | {} {}".\
                      format(os.path.dirname(src.path), src.path,
                             dest, src.path, dest, self.debugfs,
                             self._get_part_image(pnum))
        else: # fat
            if isinstance(src, str):
                cmd = "{} -i {} -snop {} ::{}".format(self.mcopy,
                                                  self._get_part_image(pnum),
                                                  src, dest.path)
            else:
                cmd = "{} -i {} -snop ::{} {}".format(self.mcopy,
                                                  self._get_part_image(pnum),
                                                  src.path, dest)

        exec_cmd(cmd, as_shell=True)
        self._put_part_image(pnum)

    def remove_ext(self, pnum, path, recursive):
        """
        Remove files/dirs and their contents from the partition.
        This only applies to ext* partition.
        """
        abs_path = re.sub(r'\/\/+', '/', path)
        cmd = "{} {} -wR 'rm \"{}\"'".format(self.debugfs,
                                            self._get_part_image(pnum),
                                            abs_path)
        out = exec_cmd(cmd , as_shell=True)
        for line in out.splitlines():
            if line.startswith("rm:"):
                if "file is a directory" in line:
                    if recursive:
                        # loop through content and delete them one by one if
                        # flaged with -r
                        subdirs = iter(self.dir(pnum, abs_path).splitlines())
                        next(subdirs)
                        for subdir in subdirs:
                            dir = subdir.split(':')[1].split(" ", 1)[1]
                            if not dir == "." and not dir == "..":
                                self.remove_ext(pnum, "%s/%s" % (abs_path, dir), recursive)

                    rmdir_out = exec_cmd("{} {} -wR 'rmdir \"{}\"'".format(self.debugfs,
                                                    self._get_part_image(pnum),
                                                    abs_path.rstrip('/'))
                                                    , as_shell=True)

                    for rmdir_line in rmdir_out.splitlines():
                        if "directory not empty" in rmdir_line:
                            raise WimError("Could not complete operation: \n%s \n"
                                            "use -r to remove non-empty directory" % rmdir_line)
                        if rmdir_line.startswith("rmdir:"):
                            raise WimError("Could not complete operation: \n%s "
                                            "\n%s" % (str(line), rmdir_line))

                else:
                    raise WimError("Could not complete operation: \n%s "
                                    "\nUnable to remove %s" % (str(line), abs_path))

    def remove(self, pnum, path, recursive):
        """Remove files/dirs from the partition."""
        partimg = self._get_part_image(pnum)
        if self.partitions[pnum].fstype.startswith('ext'):
            self.remove_ext(pnum, path, recursive)

        else: # fat
            cmd = "{} -i {} ::{}".format(self.mdel, partimg, path)
            try:
                exec_cmd(cmd)
            except WimError as err:
                if "not found" in str(err) or "non empty" in str(err):
                    # mdel outputs 'File ... not found' or 'directory .. non empty"
                    # try to use mdeltree as path could be a directory
                    cmd = "{} -i {} ::{}".format(self.mdeltree,
                                                 partimg, path)
                    exec_cmd(cmd)
                else:
                    raise err
        self._put_part_image(pnum)

    def write(self, target, expand):
        """Write disk image to the media or file."""
        def write_sfdisk_script(outf, parts):
            for key, val in parts['partitiontable'].items():
                if key in ("partitions", "device", "firstlba", "lastlba"):
                    continue
                if key == "id":
                    key = "label-id"
                outf.write("{}: {}\n".format(key, val))
            outf.write("\n")
            for part in parts['partitiontable']['partitions']:
                line = ''
                for name in ('attrs', 'name', 'size', 'type', 'uuid'):
                    if name == 'size' and part['type'] == 'f':
                        # don't write size for extended partition
                        continue
                    val = part.get(name)
                    if val:
                        line += '{}={}, '.format(name, val)
                if line:
                    line = line[:-2] # strip ', '
                if part.get('bootable'):
                    line += ' ,bootable'
                outf.write("{}\n".format(line))
            outf.flush()

        def read_ptable(path):
            out = exec_cmd("{} -J {}".format(self.sfdisk, path))
            return json.loads(out)

        def write_ptable(parts, target):
            with tempfile.NamedTemporaryFile(prefix="wim-sfdisk-", mode='w') as outf:
                write_sfdisk_script(outf, parts)
                cmd = "{} --no-reread {} < {} ".format(self.sfdisk, target, outf.name)
                exec_cmd(cmd, as_shell=True)

        if expand is None:
            sparse_copy(self.imagepath, target)
        else:
            # copy first sectors that may contain bootloader
            sparse_copy(self.imagepath, target, length=2048 * self._lsector_size)

            # copy source partition table to the target
            parts = read_ptable(self.imagepath)
            write_ptable(parts, target)

            # get size of unpartitioned space
            free = None
            for line in exec_cmd("{} -F {}".format(self.sfdisk, target)).splitlines():
                if line.startswith("Unpartitioned space ") and line.endswith("sectors"):
                    free = int(line.split()[-2])
                    # Align free space to a 2048 sector boundary. YOCTO #12840.
                    free = free - (free % 2048)
            if free is None:
                raise WimError("Can't get size of unpartitioned space")

            # calculate expanded partitions sizes
            sizes = {}
            num_auto_resize = 0
            for num, part in enumerate(parts['partitiontable']['partitions'], 1):
                if num in expand:
                    if expand[num] != 0: # don't resize partition if size is set to 0
                        sectors = expand[num] // self._lsector_size
                        free -= sectors - part['size']
                        part['size'] = sectors
                        sizes[num] = sectors
                elif part['type'] != 'f':
                    sizes[num] = -1
                    num_auto_resize += 1

            for num, part in enumerate(parts['partitiontable']['partitions'], 1):
                if sizes.get(num) == -1:
                    part['size'] += free // num_auto_resize

            # write resized partition table to the target
            write_ptable(parts, target)

            # read resized partition table
            parts = read_ptable(target)

            # copy partitions content
            for num, part in enumerate(parts['partitiontable']['partitions'], 1):
                pnum = str(num)
                fstype = self.partitions[pnum].fstype

                # copy unchanged partition
                if part['size'] == self.partitions[pnum].size // self._lsector_size:
                    logger.info("copying unchanged partition {}".format(pnum))
                    sparse_copy(self._get_part_image(pnum), target, seek=part['start'] * self._lsector_size)
                    continue

                # resize or re-create partitions
                if fstype.startswith('ext') or fstype.startswith('fat') or \
                   fstype.startswith('linux-swap'):

                    partfname = None
                    with tempfile.NamedTemporaryFile(prefix="wim-part{}-".format(pnum)) as partf:
                        partfname = partf.name

                    if fstype.startswith('ext'):
                        logger.info("resizing ext partition {}".format(pnum))
                        partimg = self._get_part_image(pnum)
                        sparse_copy(partimg, partfname)
                        exec_cmd("{} -pf {}".format(self.e2fsck, partfname))
                        exec_cmd("{} {} {}s".format(\
                                 self.resize2fs, partfname, part['size']))
                    elif fstype.startswith('fat'):
                        logger.info("copying content of the fat partition {}".format(pnum))
                        with tempfile.TemporaryDirectory(prefix='wim-fatdir-') as tmpdir:
                            # copy content to the temporary directory
                            cmd = "{} -snompi {} :: {}".format(self.mcopy,
                                                               self._get_part_image(pnum),
                                                               tmpdir)
                            exec_cmd(cmd)
                            # create new msdos partition
                            label = part.get("name")
                            label_str = "-n {}".format(label) if label else ''

                            cmd = "{} {} -C {} {}".format(self.mkdosfs, label_str, partfname,
                                                          part['size'])
                            exec_cmd(cmd)
                            # copy content from the temporary directory to the new partition
                            cmd = "{} -snompi {} {}/* ::".format(self.mcopy, partfname, tmpdir)
                            exec_cmd(cmd, as_shell=True)
                    elif fstype.startswith('linux-swap'):
                        logger.info("creating swap partition {}".format(pnum))
                        label = part.get("name")
                        label_str = "-L {}".format(label) if label else ''
                        out = exec_cmd("{} --probe {}".format(self.blkid, self._get_part_image(pnum)))
                        uuid = out[out.index("UUID=\"")+6:out.index("UUID=\"")+42]
                        uuid_str = "-U {}".format(uuid) if uuid else ''
                        with open(partfname, 'w') as sparse:
                            os.ftruncate(sparse.fileno(), part['size'] * self._lsector_size)
                        exec_cmd("{} {} {} {}".format(self.mkswap, label_str, uuid_str, partfname))
                    sparse_copy(partfname, target, seek=part['start'] * self._lsector_size)
                    os.unlink(partfname)
                elif part['type'] != 'f':
                    logger.warning("skipping partition {}: unsupported fstype {}".format(pnum, fstype))

def wim_ls(args, native_sysroot):
    """List contents of partitioned image or vfat partition."""
    disk = Disk(args.path.image, native_sysroot)
    if not args.path.part:
        if disk.partitions:
            print('Num     Start        End          Size      Fstype')
            for part in disk.partitions.values():
                print("{:2d}  {:12d} {:12d} {:12d}  {}".format(\
                          part.pnum, part.start, part.end,
                          part.size, part.fstype))
    else:
        path = args.path.path or '/'
        print(disk.dir(args.path.part, path))

def wim_cp(args, native_sysroot):
    """
    Copy file or directory to/from the vfat/ext partition of
    partitioned image.
    """
    if isinstance(args.dest, str):
        disk = Disk(args.src.image, native_sysroot)
    else:
        disk = Disk(args.dest.image, native_sysroot)
    disk.copy(args.src, args.dest)


def wim_rm(args, native_sysroot):
    """
    Remove files or directories from the vfat partition of
    partitioned image.
    """
    disk = Disk(args.path.image, native_sysroot)
    disk.remove(args.path.part, args.path.path, args.recursive_delete)

def wim_write(args, native_sysroot):
    """
    Write image to a target device.
    """
    disk = Disk(args.image, native_sysroot, ('fat', 'ext', 'linux-swap'))
    disk.write(args.target, args.expand)
