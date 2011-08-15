#!/usr/bin/python
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

import errno
import fuse
import os
import re
import stat
import subprocess
import sys
import threading
import time

try:
    import sqlite3dbm.sshelve as shelve
except ImportError:
    import shelve
import logging
import collections
import random
import cStringIO as StringIO

from fuseutils import FileProxy, FuseFile, LoopbackFile, Stat


_fuse_main = fuse.main
def fuse_main(**kwargs):
    logging.debug('%s', kwargs)
    return _fuse_main(**kwargs)
fuse.main = fuse_main


fuse.fuse_python_api = (0, 2)

SUBTITLE_EXTS = ('srt', 'sub', 'idx', 'ssa', 'ass',)
#~ VIDEO_EXTS = ('mkv', 'avi',)
VIDEO_EXTS = ('mkv',) # only mkv supported
SUPPORTED_SUBS = ('srt', 'ssa', 'ass',)

TEMP_NAME = 'tmp'
CACHEDB_NAME = 'subtitlesfs.db'
CACHE_DIR = '/tmp/.subtitlesfs'
CACHEDB = os.path.join(CACHE_DIR, CACHEDB_NAME)
TEMP_DIR = os.path.join(CACHE_DIR, TEMP_NAME)

_os_makedirs = os.makedirs
def makedirs(dirpath, *args, **kwargs):
    if os.path.isdir(dirpath):
        return
    return _os_makedirs(dirpath, *args, **kwargs)
os.makedirs = makedirs

def executable_in_path(exename):
    for path in os.getenv('PATH', '').split(':'):
        exepath = os.path.join(path, exename)
        if os.path.isfile(exepath) or os.access(exepath, os.R_OK|os.X_OK):
            return True
    return False

def genlines(file):
    line = file.readline()
    while line is not None:
        yield line
        line = file.readline()


class MkvFile(object):
    comma_split = re.compile(r"\s*,\s*(?![^\(]+?\))")
    SUBEXT_MIME_MAP = {
        'srt': 'S_TEXT/UTF8',
        'ssa': 'S_TEXT/SSA',
        'ass': 'S_TEXT/ASS',
        'sub': 'S_VOBSUB',
    }
    SUBMIME_EXT_MAP = dict([i[::-1] for i in SUBEXT_MIME_MAP.items()])
    
    def __init__(self, path):
        self.path = path
        self.logger = logging.getLogger('mkvfile')
    
    def info(self, ignore_errors=True):
        mkv_path = self.path
        cmd = ('mkvinfo', '-s')
        self._info = info = []
        stdout = None
        p = None
        
        # Return cached info if exists
        if info:
            return info
        
        cmd += (mkv_path,)
        if sys.version_info[:2] < (2, 5):
            import popen2
            p = popen2.Popen4(cmd)
            stdout = p.tochild
        if sys.version_info[:2] < (3, 0):
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, close_fds=True)
            stdout = p.stdout
        else:
            raise RuntimeError('Unsupported version of python')
        
        kvdelim = re.compile(':\s*')
        for tnum, line in enumerate(genlines(stdout)):
            line = line.rstrip()
            if line.startswith("Track"):
                try:
                    kvpairs = [re.split(kvdelim, i, 1) for i in re.split(self.comma_split, line)]
                    tinfo = dict(kvpairs)
                    # The track type is always found as the value of the first pair
                    tinfo['type'] = kvpairs[0][1]
                    info.append(tinfo)
                except Exception, e:
                    logging.error('mkv.info: %r', re.split(self.comma_split, line))
                    logging.error('mkv.info: %r', [re.split(kvdelim, i, 1) for i in re.split(self.comma_split, line)])
                    if ignore_errors:
                        logging.exception("Caught exception for track %s of %s:\n%r", tnum+1, mkv_path, line)
                        continue
                    else:
                        raise
            else:
                # The first non-Track line means no more tracks
                break
        
        # Cleanup any left-over processes
        self._cleanup_child_process(p)
        
        return info
    
    def get_subtitle_track_num(self, stype, lang='eng'):
        info = self.info()
        for i, trackinfo in enumerate(info):
            if trackinfo['type'] == 'subtitles' \
                    and trackinfo['codec ID'] == self.SUBEXT_MIME_MAP.get(stype, None) \
                    and trackinfo['language'] == lang:
                return i+1
        return 0
    
    def has_subtitle(self, stype, lang='eng'):
        return self._get_subtitle_track_num > 0 and True or False
    
    #~ def get_subtitle_names(self, )
    
    def extract(self, tracknum):
        mkv_path = self.path
        cmd = ('mkvextract', 'tracks', '-r', '/dev/null')
        stdout = None
        
        # Make sure the TEMP_DIR is created.
        if not os.path.exists(TEMP_DIR):
            os.makedirs(TEMP_DIR)
        
        mkvdir, mkvname = os.path.split(mkv_path)
        mkvbasename, mkvext = os.path.splitext(mkvname)
        subext = self.SUBMIME_EXT_MAP[self.info()[tracknum-1]['codec ID']]
        
        #~ tmpname = '%s.%s.%s.%s' % (os.getpid(), time.time(), random.randint(0, 2<<32), subext)
        tmpname = '%s.%s.%s' % (os.getpid(), mkvbasename, subext)
        tmppath = os.path.join(TEMP_DIR, tmpname)
        cmd += (mkv_path, '%s:%s'%(tracknum, tmppath))
        #~ self.logger.debug('cmd: %s', cmd)
        
        if not os.path.exists(tmppath):
            p = None
            if sys.version_info[:2] < (2, 5):
                import popen2
                p = popen2.Popen4(cmd)
                stdout = p.tochild
            if sys.version_info[:2] < (3, 0):
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, close_fds=True)
                stdout = p.stdout
            else:
                raise RuntimeError('Unsupported version of python')
            out = stdout.read()
            
            # Cleanup any left-over processes
            self._cleanup_child_process(p)
        
        data = open(tmppath).read()
        os.unlink(tmppath)
        
        return data
    
    def _cleanup_child_process(self, p):
        if p:
            # SIGKILL
            os.kill(p.pid, 9)
            os.waitpid(p.pid, 0)
            del p


# This thread extracts subtitles from video files and caches them in a
# temporary directory.
class SubtitleExtractorThread(threading.Thread):
    SLEEP_BETWEEN_SCANS_SECS = 60.
    
    def __init__(self, root, lang='eng'):
        self.logger = logging.getLogger('extractor')
        self.logger.info('init thread')
        threading.Thread.__init__(self)
        self.root = root
        self.lang = lang
        self.immediate_extraction = collections.deque()
        #~ self.condition = condition
        
    def run(self):
        try:
            self._run()
        except Exception, e:
            self.logger.exception("Exception caught in extractor thread")
            raise
    
    def _run(self):
        self.logger.info('running extractor thread: %s', self.root)
        while True:
            # continually scan root
            for path, dirs, files in os.walk(self.root):
                for file in files:
                    # hook to make sure requests from the filesystem get
                    # handled
                    self.do_immediate_extraction()
                    
                    fullpath = os.path.join(path, file)
                    basename, ext = os.path.splitext(file)
                    ext = ext[1:]
                    
                    self.logger.debug("Thinking about extracting: %s", fullpath)
                    if ext in VIDEO_EXTS:
                        self.extract_subs(fullpath)
            
            time.sleep(self.SLEEP_BETWEEN_SCANS_SECS)
    
    def do_immediate_extraction(self):
        while self.immediate_extraction:
            try:
                mkvpath = self.immediate_extraction.popleft()
                self.extract_subs(mkvpath)
            except IndexError:
                return
    
    def extract_subs(self, mkvpath):
        """ """
        try:
            self.extract_and_cache_subs(mkvpath, self.lang, self.logger)
        except Exception, e:
            logging.exception("Exception during extraction of subtitles from %s"%mkvpath)
    
    @staticmethod
    def extract_and_cache_subs(mkvpath, lang, logger=logging):
        cached_subs = []
        basepath, ext = os.path.splitext(mkvpath)
        mkv = MkvFile(mkvpath)
        for tnum, trackinfo in enumerate(mkv.info()):
            if trackinfo['type'] == 'subtitles' \
                    and trackinfo.get('language', 'eng') == lang:
                logger.debug('mkv track info: %r', trackinfo)
                subext = mkv.SUBMIME_EXT_MAP.get(trackinfo['codec ID'], None)
                if subext in SUPPORTED_SUBS:
                    fullpath = os.path.join(CACHE_DIR, '.'.join([basepath.lstrip('/'), subext]))
                    
                    if os.path.isfile(fullpath) \
                        and os.lstat(mkvpath).st_mtime == os.lstat(fullpath).st_mtime:
                        # mkv has not changed and subfile exists
                        return
                    
                    # Make sure the path is created
                    fullpath_dirname = os.path.dirname(fullpath)
                    os.makedirs(fullpath_dirname)
                    
                    logger.debug('Writing %s to cache', fullpath)
                    subdata = mkv.extract(tnum+1)
                    open(fullpath, 'w').write(subdata)
                    
                    # Set access and modification time on sub file to same
                    # as on mkv, so if mkv changes we know to update the
                    # sub.
                    mkvstat = os.lstat(mkvpath)
                    logger.debug('Setting mtime: %s', mkvstat.st_mtime)
                    os.utime(fullpath, (mkvstat.st_atime, mkvstat.st_mtime))
                    
                    cached_subs.append(fullpath)
        return cached_subs
    
    def cleanup(self):
        """ Remove cached subtitles with no video file """
        raise NotImplementedError


class SubStat(Stat):
    pass


class SubFile(FuseFile):
    def __init__(self, *args, **kwargs):
        self.root = kwargs.pop('root')
        self.fuse = kwargs.pop('fuse')
        # Bypass the fs cache, which was giving errors.
        self.direct_io = True
        
        super(SubFile, self).__init__(*args, **kwargs)
        self.abspath = os.path.join(self.root.rstrip('/'), self.path.lstrip('/'))
        
        # This didn't seem to invalidate the cache, or at least I'm still
        # getting read errors, which disappear when using direct_io = True
        #~ self.fuse.Invalidate(self.path)
        #~ self.fuse.Invalidate(self.abspath)
        
        self.extract_subfiles()
    
    def extract_subfiles(self):
        if not self.file:
            base, ext = os.path.splitext(self.abspath)
            ext = ext[1:]
            
            if ext.lower() in SUBTITLE_EXTS:
                # Trying to access a "virtual" subtitle file
                basedir, basename = os.path.split(base)
                
                self.logger.debug('>> %s %s', basename, os.listdir(basedir))
                # Get potential video file matches
                potential_match = [f for f in os.listdir(basedir)
                                    if f.startswith(basename) \
                                        and os.path.splitext(f)[1][1:].lower() in VIDEO_EXTS]
                assert len(potential_match) < 2, 'More than one potential match: %s'%potential_match
                
                self.logger.debug('potential_match: %s', potential_match)
                if len(potential_match) == 0:
                    return -errno.ENOENT
                
                mkv_path = os.path.join(basedir, potential_match[0])
                
                subpaths = SubtitleExtractorThread.extract_and_cache_subs(
                    mkv_path, self.lang, logger
                )
                
                if os.path.exists(self.fullpath):
                    self.file = open(self.fullpath, 'rb')
                    self.fd = self.file.fileno()
    
    #~ def write(self, buf, offset):
        #~ path = self.path
        #~ self.logger.info("write: %s %s %s", path, buf, offset)
        #~ return -errno.EROFS
    
    def read(self, size, offset):
        path = self.path
        abspath = self.abspath
        self.logger.info("read: %s %s %s", path, size, offset)
        
        try:
            self.file.seek(offset)
            data = self.file.read(size)
            #~ data = self.file.read(size-(size%(4*4*1024))-1)
            self.logger.debug("read: return %s %r %r", len(data), data[:20], data[-20:])
            return data
        except Exception, e:
            self.logger.exception('Exception while reading')
    
    def fgetattr(self):
        self.logger.info("fgetattr %s %s %s", self.path, self.abspath, self.fullpath)
        try:
            self.fuse.getattr(self.path)
        except Exception, e:
            self.logger.exception('fuse.getattr raise exception')
            raise


# Supposedly a better way to get the fuse instance passed to the file_class
# But I don't see how it could work.
# http://sourceforge.net/apps/mediawiki/fuse/index.php?title=FUSE_Python_Reference#File_Class_Methods
#~ class wrapped_file_class(SubFile):
    #~ def __init__(self2, *a, **kw):
        #~ my_file_class__init__(self2, self, *a, **kw)
#~ SubFile = wrapped_file_class


class SubtitleFileProxy(FileProxy):
    root = None
    fuse = None
    
    def multiplex(self, path, flags, *mode, **kwargs):
        logging.debug('proxy.multiplex: %s %s', path, flags)
        base, ext = os.path.splitext(path)
        ext = ext[1:]
        try:
            if ext.lower() in SUPPORTED_SUBS:
                file = SubFile(path, flags, fuse=self.fuse, root=self.root,
                            prefix=os.path.join(CACHE_DIR, self.root.lstrip('/')))
            else:
                #~ file = open(path, mode)
                file = LoopbackFile(path, flags, prefix=self.root)
                logging.debug('read in open: %r', file.read(10))
                #~ file = fuse.FuseFileInfo(direct_io=True)
        except Exception, e:
            logging.exception('Got exception in multiplex')
            raise
        logging.debug('multiplex -> %s', file)
        return file


class SubsFuse(fuse.Fuse):
    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)
        
        # Set defaults value, which can be overridden by commandline opts.
        self.root = '/'
        self.lang = 'eng'
        self.log = self.loglevel = self.cachedir = None
        self.use_cache_only = False
    
    def main(self, *args, **kwargs):
        # Setup the logging here, which should be as soon as possible
        # after parsing the command line options
        logging_opts = dict(stream = sys.stderr)
        if self.log:
            logging_opts = dict(filename = self.log)
        if self.loglevel:
            logging_opts['level'] = getattr(logging, self.loglevel.upper())
        logging.basicConfig(**logging_opts)
        
        self.logger = logging.getLogger('fuse')
        
        # Add open callback for fileclass multiplexing
        #~ kwargs.setdefault('open', open_cb)
        
        # Add proxy class for choosing
        self.file_class = SubtitleFileProxy
        SubtitleFileProxy.fuse = self
        SubtitleFileProxy.root = self.root
        
        super(SubsFuse, self).main(*args, **kwargs)
    
    def fsinit(self):
        # Reset set globals depending on the value of cache dir
        if self.cachedir:
            global CACHE_DIR
            global CACHEDB
            global TEMP_DIR
            CACHE_DIR = self.cachedir
            CACHEDB = os.path.join(CACHE_DIR, CACHEDB_NAME)
            TEMP_DIR = os.path.join(CACHE_DIR, TEMP_NAME)
        
        # Don't start the extractor thread if told to only use cache
        if not self.use_cache_only:
            self.t = t = SubtitleExtractorThread(self.root, self.lang)
            t.setDaemon(True)
            t.start()
    
    def fsdestroy(self):
        logging.shutdown()
    
    def getattr(self, path):
        #~ if self.icase:
            #~ path = path.lower()
        abspath = os.path.join(self.root, path.lstrip('/'))
        cachepath = os.path.join(CACHE_DIR, abspath.lstrip('/'))
        self.logger.info("getattr %s %s %s", path, abspath, cachepath)
        if os.path.exists(abspath):
            sub_stat = os.lstat(abspath)
        elif os.path.exists(cachepath):
            sub_stat = os.lstat(cachepath)
        else:
            base, ext = os.path.splitext(abspath)
            ext = ext[1:]
            
            if ext.lower() in SUBTITLE_EXTS:
                # Trying to access a "virtual" subtitle file
                basedir, basename = os.path.split(base)
                
                self.logger.debug('>> %s %s', basename, os.listdir(basedir))
                # Get potential video file matches
                potential_match = [f for f in os.listdir(basedir)
                                    if f.startswith(basename) \
                                        and os.path.splitext(f)[1][1:].lower() in VIDEO_EXTS]
                assert len(potential_match) < 2, 'More than one potential match: %s'%potential_match
                
                self.logger.debug('potential_match: %s', potential_match)
                if len(potential_match) == 0:
                    return -errno.ENOENT
                
                # The subtitle was not in the cache, so tell the extractor
                # to get it next.
                try:
                    mkv_path = os.path.join(basedir, potential_match[0])
                    mkv_stat = os.lstat(mkv_path)
                    mkv = MkvFile(mkv_path)
                    tnum = mkv.get_subtitle_track_num(ext, self.lang)
                    if tnum == 0:
                        # Track not found for this sub type and language
                        return -errno.ENOENT
                    subdata = mkv.extract(tnum)
                except Exception, e:
                    #~ import traceback
                    #~ traceback.print_exc()
                    self.logger.exception("Logged exception while trying to extract subtitles from %s", mkv_path)
                    raise
                
                sub_stat = SubStat(mkv_stat)
                sub_stat.st_size = len(subdata)
                
            else:
                # Either not a subtitle access or no such subtitle existed in
                # the video file
                return -errno.ENOENT
        
        self.logger.debug('sub_stat: %s', sub_stat)
        return sub_stat
    
    def readdir(self, path, offset):
        abspath = os.path.join(self.root.rstrip('/'), path.lstrip('/'))
        self.logger.info("readdir: %s %s", path, offset)
        for e in os.listdir(abspath)[offset:]:
            yield fuse.Direntry(e)
            
            basename, ext = os.path.splitext(e)
            ext = ext[1:]
            if ext.lower() in VIDEO_EXTS:
                mkv = MkvFile(os.path.join(abspath, e))
                for trackinfo in mkv.info(ignore_errors=True):
                    if trackinfo['type'] == 'subtitles' \
                            and trackinfo.get('language', None) == self.lang:
                        self.logger.debug('mkv track info: %s', trackinfo)
                        subext = mkv.SUBMIME_EXT_MAP[trackinfo['codec ID']]
                        if subext in SUPPORTED_SUBS:
                            yield fuse.Direntry('.'.join([basename, subext]))
                            #~ yield fuse.Direntry('.'.join([basename, subext]), type=stat.S_IFREG)
    
    def mknod(self, path, mode, dev):
        self.logger.info("mknod: %s %s %s", path, mode, dev)
        return -errno.EROFS

    def write(self, path, buf, offset):
        self.logger.info("write: %s %s %s", path, buf, offset)
        return -errno.EROFS


def main():
    if not executable_in_path('mkvinfo'):
        print >> sys.stderr, "mkvtoolnix must be installed and in your PATH."
        sys.exit(1)
    
    server = SubsFuse(version="%prog " + fuse.__version__,
                      usage="Run with './subtitlefs -s -f <mount_point>' "
                            "to start subtitlefs",
                      dash_s_do="setsingle")
    
    # FIXME: ensure code is thread safe before turning this on
    server.multithreaded = True
    #~ server.multithreaded = False
    
    server.parser.add_option(mountopt='root', metavar="PATH", default='/',
                             help="show subtitles in videos under PATH [default: %default]")
    server.parser.add_option(mountopt='lang', metavar="LANG", default='eng',
                             help="show subtitles with language LANG [default: %default]")
    #~ server.parser.add_option(mountopt='numthreads', metavar='NUM', default=1,
                             #~ help="set number of threads [default: %default]")
    #~ server.parser.add_option(mountopt='icase', default=False, action='store_true',
                             #~ help="set case insensitivity [default: %default]")
    server.parser.add_option(mountopt='use_cache_only', default=False, action='store_true',
                             help="only use cached subs, do not run extracting thread [default: %default]")
    server.parser.add_option(mountopt='log', metavar='FILE', default=None,
                             help="log to FILE [default: %default]")
    server.parser.add_option(mountopt='loglevel', metavar='LEVEL', default=None,
                             help="set logging to LEVEL [default: %default]")
    server.parser.add_option(mountopt='cachedir', metavar='CACHE_DIR', default=CACHE_DIR,
                             help="set cache directory [default: %default]")
    
    server.parse(values=server, errex=1)
    
    try:
        if server.fuse_args.mount_expected():
            os.chdir(server.root)
    except OSError:
        print >> sys.stderr, "can't enter root of underlying filesystem"
        sys.exit(1)
    
    server.main()

if __name__ == "__main__":
    main()
