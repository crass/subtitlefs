# -*- coding: utf-8 -*-

# Add subtitle files in the same directory as videos containing the subtitles
# with the same name, but apropriate subtitle extension.
# Copyright 2011 crass <crass@berlios.de>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#   2. Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#   3. The name of the author may not be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import errno
import functools
import logging
import fuse


def flag2mode(flags):
    md = {os.O_RDONLY: 'rb', os.O_WRONLY: 'wb', os.O_RDWR: 'wb+'}
    m = md[flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)]

    #~ if flags | os.O_APPEND:
    if (flags & os.O_APPEND) == os.O_APPEND:
        m = m.replace('w', 'a', 1)

    return m


class AttrNotImplemented(type):
    def __getattr__(self, attr):
        def meth(self, *args, **kwargs):
            raise NotImplementedError()
        return meth

# TODO: It might be interesting to write a wraps, which look at
#       inspect.getargspec to get the wrapped functions method sig
#       and create a wrapper which matches that. There would need to be
#       some byte code generation.

class LoggerMetaclass(type):
    """ Metaclass to provide a logger attribute to the instance whose name is the class name. """
    def __new__(mcs, name, bases, dict):
        f = dict.pop('__init__', None)
        if f:
            @functools.wraps(f)
            def __init__wrapper(self, *args, **kwargs):
                if not hasattr(self, 'logger'):
                    self.logger = logging.getLogger(name)
                f(self, *args, **kwargs)
            dict['__init__'] = __init__wrapper
        else:
            # The class has no __init__ defined, so define one
            def __init__(self, *args, **kwargs):
                if not hasattr(self, 'logger'):
                    self.logger = logging.getLogger(name)
                super(self.__class__, self).__init__(*args, **kwargs)
            dict['__init__'] = __init__
        
        return type.__new__(mcs, name, bases, dict)


class FileProxy(object):
    __metaclass__ = AttrNotImplemented
    
    def __init__(self, path, *args, **kwargs):
        self.__dict__['fileproxy'] = self.multiplex(path, *args, **kwargs)
    
    def __getattr__(self, attr):
        logging.debug('FileProxy.getattr: %s', attr)
        return getattr(self.fileproxy, attr)
    
    def __setattr__(self, attr, value):
        return setattr(self.fileproxy, attr, value)
    
    def multiplex(self, path, flags, *mode, **kwargs):
        raise NotImplementedError("Implement in subclass to return file class for this path")


class FuseFile(object):
    __metaclass__ = LoggerMetaclass
    prefix = '.'
    
    def __init__(self, path, flags, *mode, **kwargs):
        self.prefix = kwargs.pop('prefix', self.prefix)
        self.path = path
        self.flags = flags
        self.mode = mode
        self.fullpath = self.prefix + path
        if os.path.exists(self.fullpath):
            self.file = os.fdopen(os.open(self.fullpath, flags, *mode),
                                  flag2mode(flags))
            self.fd = self.file.fileno()
        else:
            self.file = None
    
    def release(self, flags):
        if self.file:
            self.file.close()
    
    def _fflush(self):
        if self.file and 'w' in self.file.mode or 'a' in self.file.mode:
            self.file.flush()
    
    def flush(self):
        self._fflush()
        # cf. xmp_flush() in fusexmp_fh.c
        if self.file:
            os.close(os.dup(self.fd))
    
    def lock(self, cmd, owner, **kw):
        self.logger.debug('lock: %s %s %s', cmd, owner, kw)
        return 0
        #~ return -errno.EINVAL


class LoopbackFile(FuseFile):
    def read(self, length, offset=0):
        self.file.seek(offset)
        return self.file.read(length)

    def write(self, buf, offset):
        self.file.seek(offset)
        self.file.write(buf)
        return len(buf)

    def release(self, flags):
        self.file.close()
    
    def _fflush(self):
        if 'w' in self.file.mode or 'a' in self.file.mode:
            self.file.flush()

    def fsync(self, isfsyncfile):
        self._fflush()
        if isfsyncfile and hasattr(os, 'fdatasync'):
            os.fdatasync(self.fd)
        else:
            os.fsync(self.fd)

    def fgetattr(self):
        return os.fstat(self.fd)

    def ftruncate(self, len):
        self.file.truncate(len)


class Stat(fuse.Stat):
    st_attrs = ('st_mode', 'st_ino', 'st_dev', 'st_nlink', 'st_uid',
                'st_gid', 'st_size', 'st_atime', 'st_mtime', 'st_ctime',)
    
    def __init__(self, copy_stat=None, **kwargs):
        if copy_stat:
            # fuse.Stat.__init__ will overwrite any attrs we set now,
            # so set the attrs in the keywords.
            for attr in self.st_attrs:
                # Use set default so that original keys take precendence
                kwargs.setdefault(attr, getattr(copy_stat, attr))
            
        super(Stat, self).__init__(**kwargs)
    
    def __repr__(self):
        sattrs = ', '.join(['%s=%s'%(attr, getattr(self, attr))
                                for attr in self.st_attrs])
        return '<%s %s>'%(self.__class__.__name__, sattrs)
    
    def copy(self, copy_stat):
        for attr in self.st_attrs:
            setattr(self, attr, getattr(copy_stat, attr, None))

