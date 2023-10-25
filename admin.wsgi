"""
AdminTool: Web app for IF Archive administration.

See tinyapp/app.py for how the underlying web app framework works.

This file (admin.wsgi) is installed as /var/ifarchive/wsgi-bin/admin.wsgi.
It can also be run to perform some command-line operations:

  python3 /var/ifarchive/wsgi-bin/admin.wsgi
"""

import sys
import time
import os, os.path
import hashlib
import configparser
import logging, logging.handlers


# The config file contains all the paths and settings used by the app.

configpath = '/Users/zarf/src/ifarch/ifarchive-admintool/test.config'
#configpath = '/var/ifarchive/lib/ifarch.config'
config = configparser.ConfigParser()
config.read(configpath)

# Set up PyLibPath before we start importing tinyapp modules
if 'PyLibPath' in config['AdminTool']:
    val = config['AdminTool']['PyLibPath']
    if val not in sys.path:
        sys.path.append(val)

# Set up the logging configuration
loghandler = logging.handlers.WatchedFileHandler(config['AdminTool']['LogFile'])
logging.basicConfig(
    format = '[%(levelname).1s %(asctime)s] %(message)s',
    datefmt = '%b-%d %H:%M:%S',
    level = logging.INFO,
    handlers = [ loghandler ],
)

from tinyapp.constants import PLAINTEXT, BINARY
from tinyapp.handler import before, beforeall
from tinyapp.excepts import HTTPError, HTTPRedirectPost, HTTPRawResponse
from tinyapp.util import random_bytes, time_now

from adminlib.session import User, Session
from adminlib.session import require_user, require_role
from adminlib.util import bad_filename, in_user_time, read_md5, read_size
from adminlib.util import zip_compress
from adminlib.util import find_unused_filename
from adminlib.info import FileEntry, UploadEntry
from adminlib.admapp import AdminApp, AdminHandler

    
# URL handlers...

class han_Home(AdminHandler):
    renderparams = { 'navtab':'top' }
    
    def do_get(self, req):
        if not req._user:
            return self.render('login.html', req)

        incount = len([ ent for ent in os.scandir(self.app.incoming_dir) if ent.is_file() ])
        unproccount = len([ ent for ent in os.scandir(self.app.unprocessed_dir) if ent.is_file() ])

        return self.render('front.html', req, incount=incount, unproccount=unproccount)

    def do_post(self, req):
        formname = req.get_input_field('name')
        formpw = req.get_input_field('password')

        if not (formname and formpw):
            return self.render('login.html', req,
                                   formerror='You must supply name and password.')
        
        curs = self.app.getdb().cursor()

        if '@' in formname:
            res = curs.execute('SELECT name, pw, pwsalt, roles FROM users WHERE email = ?', (formname,))
        else:
            res = curs.execute('SELECT name, pw, pwsalt, roles FROM users WHERE name = ?', (formname,))
        tup = res.fetchone()
        if not tup:
            return self.render('login.html', req,
                                   formerror='The name and password do not match.')
        
        name, pw, pwsalt, roles = tup
        formsalted = pwsalt + b':' + formpw.encode()
        formcrypted = hashlib.sha1(formsalted).hexdigest()
        if formcrypted != pw:
            return self.render('login.html', req,
                                   formerror='The name and password do not match.')

        ### set name cookie for future logins? (filled into login.html form)

        sessionid = random_bytes(20)
        req.set_cookie('sessionid', sessionid, maxage=self.app.max_session_age, httponly=True, secure=self.app.secure_site)
        now = time_now()
        ipaddr = req.env.get('REMOTE_ADDR', '?')
        
        curs = self.app.getdb().cursor()
        curs.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?)', (name, sessionid, ipaddr, now, now))
        
        req.loginfo('Logged in: user=%s, roles=%s', name, roles)
        raise HTTPRedirectPost(self.app.approot)


class han_LogOut(AdminHandler):
    def do_get(self, req):
        if req._user:
            curs = self.app.getdb().cursor()
            curs.execute('DELETE FROM sessions WHERE sessionid = ?', (req._user.sessionid,))
            # Could clear the sessionid cookie here but I can't seem to make that work
        raise HTTPRedirectPost(self.app.approot)


@beforeall(require_user)
class han_UserProfile(AdminHandler):
    def do_get(self, req):
        return self.render('user.html', req)

@beforeall(require_user)
class han_ChangePW(AdminHandler):
    def do_get(self, req):
        return self.render('changepw.html', req)
    
    def do_post(self, req):
        oldpw = req.get_input_field('oldpassword')
        newpw = req.get_input_field('newpassword')
        duppw = req.get_input_field('duppassword')
        if not newpw:
            return self.render('changepw.html', req,
                                   formerror='You must supply a new password.')

        curs = self.app.getdb().cursor()
        res = curs.execute('SELECT pw, pwsalt FROM users WHERE name = ?', (req._user.name,))
        tup = res.fetchone()
        if not tup:
            return self.render('changepw.html', req,
                                   formerror='Cannot locate user record.')
        pw, pwsalt = tup
        formsalted = pwsalt + b':' + oldpw.encode()
        formcrypted = hashlib.sha1(formsalted).hexdigest()
        if formcrypted != pw:
            return self.render('changepw.html', req,
                                   formerror='Old password does not match.')
        if newpw != duppw:
            return self.render('changepw.html', req,
                                   formerror='New password does not match.')

        pwsalt = random_bytes(8).encode()
        salted = pwsalt + b':' + newpw.encode()
        crypted = hashlib.sha1(salted).hexdigest()
        curs.execute('UPDATE users SET pw = ?, pwsalt = ? WHERE name = ?', (crypted, pwsalt, req._user.name))
        
        req.loginfo('Changed password')
        return self.render('changepwdone.html', req)
            

@beforeall(require_user)
class han_ChangeTZ(AdminHandler):
    def do_get(self, req):
        return self.render('changetz.html', req)
    
    def do_post(self, req):
        tzname = req.get_input_field('tz_field')
        curs = self.app.getdb().cursor()
        curs.execute('UPDATE users SET tzname = ? WHERE name = ?', (tzname, req._user.name))
        req.loginfo('Changed timezone to %s', tzname)
        raise HTTPRedirectPost(self.app.approot+'/user')


@beforeall(require_role('admin'))
class han_AdminAdmin(AdminHandler):
    renderparams = { 'navtab':'admin' }

    def do_get(self, req):
        return self.render('admin.html', req)


@beforeall(require_role('admin'))
class han_AllUsers(AdminHandler):
    renderparams = { 'navtab':'admin' }

    def do_get(self, req):
        curs = self.app.getdb().cursor()
        res = curs.execute('SELECT name, email, roles FROM users')
        userlist = [ User(name, email, roles=roles) for name, email, roles in res.fetchall() ]
        return self.render('allusers.html', req,
                               users=userlist)


@beforeall(require_role('admin'))
class han_AllSessions(AdminHandler):
    renderparams = { 'navtab':'admin' }

    def do_get(self, req):
        curs = self.app.getdb().cursor()
        res = curs.execute('SELECT name, ipaddr, starttime, refreshtime FROM sessions')
        sessionlist = [ Session(tup, user=req._user, maxage=self.app.max_session_age) for tup in res.fetchall() ]
        return self.render('allsessions.html', req,
                               sessions=sessionlist)

    
class base_DirectoryPage(AdminHandler):
    """Base class for all handlers that display a file list.
    This will have subclasses for each directory that has special
    handling. (Incoming, Trash, etc.)
    
    This is rather long and messy because it also handles all the
    buttons that can appear under a file name: Move, Rename, Delete,
    and so on.
    """
    
    def get_dirpath(self, req):
        """Return the (full) filesystem path of the directory that this
        subclass will operate on. Subclasses must customize this.
        """
        raise NotImplementedError('%s: get_dirpath not implemented' % (self.__class__.__name__,))

    def get_file(self, filename, req):
        """Get one FileEntry from our directory, or None if the file
        does not exist.
        """
        if bad_filename(filename):
            return None
        pathname = os.path.join(self.get_dirpath(req), filename)
        if not os.path.exists(pathname):
            return None
        stat = os.stat(pathname)
        return FileEntry(filename, stat, user=req._user)
        
    def get_filelist(self, req):
        """Get a list of FileEntries from our directory.
        """
        filelist = []
        for ent in os.scandir(self.get_dirpath(req)):
            if ent.is_file():
                stat = ent.stat()
                file = FileEntry(ent.name, stat, user=req._user)
                filelist.append(file)
        filelist.sort(key=lambda file:file.date)
        return filelist

    def do_get(self, req):
        """The GET case is easy: we just show the list of files. And their
        buttons.
        """
        view = req.get_query_field('view')
        if view:
            if view == 'info':
                return self.do_get_info(req)
            if view == 'dl':
                return self.do_get_download(req)
            filename = req.get_query_field('filename')
            raise HTTPError('404 Not Found', 'View "%s" not found: %s' % (view, filename,))
        return self.render(self.template, req)

    def do_get_download(self, req):
        filename = req.get_query_field('filename')
        if bad_filename(filename):
            msg = 'Not found: %s' % (filename,)
            raise HTTPError('404 Not Found', msg)
        
        dirpath = self.get_dirpath(req)
        pathname = os.path.join(dirpath, filename)
        try:
            stat = os.stat(pathname)
            filesize = stat.st_size
        except Exception as ex:
            msg = 'Unable to stat: %s %s' % (pathname, ex,)
            raise HTTPError('400 Not Readable', msg)
        
        fl = None
        try:
            fl = open(pathname, 'rb')
        except Exception as ex:
            msg = 'Unable to read: %s %s' % (pathname, ex,)
            raise HTTPError('400 Not Readable', msg)
        
        response_headers = [
            ('Content-Type', BINARY),
            ('Content-Length', str(filesize)),
            ('Content-Disposition', 'attachment; filename="%s"' % (filename.replace('"', '_'),))
        ]
        def resp():
            while True:
                val = fl.read(8192)
                if not val:
                    break
                yield val
            fl.close()
            return
        raise HTTPRawResponse('200 OK', response_headers, resp())
    
    def do_get_info(self, req):
        filename = req.get_query_field('filename')
        if bad_filename(filename):
            msg = 'Not found: %s' % (filename,)
            raise HTTPError('404 Not Found', msg)
        pathname = os.path.join(self.get_dirpath(req), filename)

        try:
            stat = os.stat(pathname)
            filesize = stat.st_size
        except Exception as ex:
            msg = 'Unable to stat: %s %s' % (pathname, ex,)
            raise HTTPError('400 Not Readable', msg)

        if not filesize:
            # No point in checking the upload history for zero-length
            # uploads.
            uploads = []
        else:
            hashval = read_md5(pathname)
            curs = self.app.getdb().cursor()
            res = curs.execute('SELECT * FROM uploads WHERE md5 = ? ORDER BY uploadtime', (hashval,))
            uploads = [ UploadEntry(tup, user=req._user) for tup in res.fetchall() ]
            
        return self.render('uploadinfo.html', req, filename=filename, filesize=filesize, uploads=uploads)

    def do_post(self, req):
        """The POST case has to handle showing the "confirm/cancel" buttons
        after an operation is selected, and *also* the confirmed operation
        itself.
        """
        # dirname is the user-readable name (e.g. "incoming" or "unprocessed")
        dirname = self.renderparams['dirname']
        # dirpath is the filesystem path (e.g. "/var/ifarchive/incoming")
        dirpath = self.get_dirpath(req)
        # uribase is the URL element after approot (e.g. "incoming" or "arch/unprocessed")
        uribase = self.renderparams['uribase']
        
        filename = req.get_input_field('filename')
        ent = self.get_file(filename, req)
        if not ent:
            return self.render(self.template, req,
                               formerror='File not found: "%s"' % (filename,))

        # On any Cancel button, we redirect back to the GET for this page.
        if req.get_input_field('cancel'):
            raise HTTPRedirectPost(self.app.approot+'/'+uribase)

        # The operation may be defined by an "op" hidden field or by the
        # button just pressed. (Depending on what stage we're at.)
        if req.get_input_field('op'):
            op = req.get_input_field('op')
        elif req.get_input_field('delete'):
            op = 'delete'
        elif req.get_input_field('moveu'):
            op = 'moveu'
        elif req.get_input_field('movei'):
            op = 'movei'
        elif req.get_input_field('rename'):
            op = 'rename'
        elif req.get_input_field('zip'):
            op = 'zip'
        else:
            return self.render(self.template, req,
                               formerror='Invalid operation')

        # If neither "confirm" nor "cancel" was pressed, we're at the
        # stage of showing those buttons. (And also the "rename" input
        # field, etc.) Render the template with those controls showing.
        # "opfile" will be the highlighted file.
        if not req.get_input_field('confirm'):
            return self.render(self.template, req,
                               op=op, opfile=filename)

        # The "confirm" button was pressed, so it's time to perform the
        # action.
        
        if op == 'delete':
            if dirpath == self.app.trash_dir:
                raise Exception('delete op cannot be used in the trash')
            newname = find_unused_filename(filename, self.app.trash_dir)
            origpath = os.path.join(dirpath, filename)
            newpath = os.path.join(self.app.trash_dir, newname)
            os.rename(origpath, newpath)
            req.loginfo('Deleted "%s" from /%s', filename, dirname)
            return self.render(self.template, req,
                               diddelete=filename, didnewname=newname)
        
        elif op == 'moveu':
            if dirpath == self.app.unprocessed_dir:
                raise Exception('moveu op cannot be used in the unprocessed dir')
            newname = find_unused_filename(filename, self.app.unprocessed_dir)
            origpath = os.path.join(dirpath, filename)
            newpath = os.path.join(self.app.unprocessed_dir, newname)
            os.rename(origpath, newpath)
            req.loginfo('Moved "%s" from /%s to /unprocessed', filename, dirname)
            return self.render(self.template, req,
                               didmoveu=filename, didnewname=newname)
        
        elif op == 'movei':
            if dirpath == self.app.incoming_dir:
                raise Exception('movei op cannot be used in the incoming dir')
            newname = find_unused_filename(filename, self.app.incoming_dir)
            origpath = os.path.join(dirpath, filename)
            newpath = os.path.join(self.app.incoming_dir, newname)
            os.rename(origpath, newpath)
            req.loginfo('Moved "%s" from /%s to /incoming', filename, dirname)
            return self.render(self.template, req,
                               didmovei=filename, didnewname=newname)
        
        elif op == 'rename':
            newname = req.get_input_field('newname')
            if newname is not None:
                newname = newname.strip()
            if not newname:
                return self.render(self.template, req,
                                   op=op, opfile=filename,
                                   formerror='You must supply a filename.')
            if bad_filename(newname):
                return self.render(self.template, req,
                                   op=op, opfile=filename,
                                   formerror='Invalid filename: "%s"' % (newname,))
            if newname in FileEntry.specialnames:
                return self.render(self.template, req,
                                   op=op, opfile=filename,
                                   formerror='Cannot use reserved filename: "%s"' % (newname,))
            origpath = os.path.join(dirpath, filename)
            newpath = os.path.join(dirpath, newname)
            if os.path.exists(newpath):
                return self.render(self.template, req,
                                   op=op, opfile=filename,
                                   formerror='Filename already in use: "%s"' % (newname,))
            os.rename(origpath, newpath)
            req.loginfo('Renamed "%s" to "%s" in /%s', filename, newname, dirname)
            return self.render(self.template, req,
                               didrename=filename, didnewname=newname)
        
        elif op == 'zip':
            newname = filename+'.zip'
            origpath = os.path.join(dirpath, filename)
            newpath = os.path.join(dirpath, newname)
            if os.path.exists(newpath):
                return self.render(self.template, req,
                                   op=op, opfile=filename,
                                   formerror='File already exists: "%s"' % (newname,))
            origmd5 = read_md5(origpath)
            zip_compress(origpath, newpath)

            # Now create a new upload entry with the new md5.
            newsize = read_size(newpath)
            newmd5 = read_md5(newpath)
            curs = self.app.getdb().cursor()
            res = curs.execute('SELECT uploadtime, origfilename, donorname, donoremail, donorip, donoruseragent, permission, suggestdir, ifdbid, about FROM uploads where md5 = ?', (origmd5,))
            for (uploadtime, origfilename, donorname, donoremail, donorip, donoruseragent, permission, suggestdir, ifdbid, about) in list(res.fetchall()):
                curs.execute('INSERT INTO uploads (uploadtime, md5, size, filename, origfilename, donorname, donoremail, donorip, donoruseragent, permission, suggestdir, ifdbid, about) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (uploadtime, newmd5, newsize, newname, origfilename, donorname, donoremail, donorip, donoruseragent, permission, suggestdir, ifdbid, about))

            req.loginfo('Zipped "%s" to "%s" in /%s', filename, newname, dirname)
            return self.render(self.template, req,
                               didzip=filename, didnewname=newname)
            
        else:
            return self.render(self.template, req,
                               formerror='Operation not implemented: %s' % (op,))


@beforeall(require_role('incoming', 'admin'))
class han_Incoming(base_DirectoryPage):
    renderparams = {
        'navtab': 'incoming',
        'uribase': 'incoming', 'dirname': 'incoming',
        'filebuttons': set(['moveu', 'rename', 'delete', 'zip']),
    }
    template = 'incoming.html'

    def add_renderparams(self, req, map):
        map['trashcount'] = self.get_trashcount(req)
        map['files'] = self.get_filelist(req)
        return map

    def get_dirpath(self, req):
        return self.app.incoming_dir

    def get_trashcount(self, req):
        count = len([ ent for ent in os.scandir(self.app.trash_dir) if ent.is_file() ])
        return count


@beforeall(require_role('incoming', 'admin'))
class han_Trash(base_DirectoryPage):
    renderparams = {
        'navtab': 'trash',
        'uribase': 'trash', 'dirname': 'trash',
        'filebuttons': set(['movei', 'moveu', 'rename']),
    }
    template = 'trash.html'

    def add_renderparams(self, req, map):
        map['files'] = self.get_filelist(req)
        return map

    def get_dirpath(self, req):
        return self.app.trash_dir


@beforeall(require_role('incoming', 'admin'))
class han_Unprocessed(base_DirectoryPage):
    renderparams = {
        'navtab': 'unprocessed',
        'uribase': 'arch/unprocessed', 'dirname': 'unprocessed',
        'filebuttons': set(['delete', 'movei', 'rename']),
    }
    template = 'unprocessed.html'

    def add_renderparams(self, req, map):
        map['files'] = self.get_filelist(req)
        map['incomingcount'] = self.get_incomingcount(req)
        return map

    def get_dirpath(self, req):
        return self.app.unprocessed_dir

    def get_incomingcount(self, req):
        count = len([ ent for ent in os.scandir(self.app.incoming_dir) if ent.is_file() ])
        return count

def check_archive_dir(req, han):
    dirname = req.match.groupdict()['dir']
    if dirname.endswith('/'):
        dirname = dirname[ : -1 ]
        if not dirname:
            raise HTTPRedirectPost(req.app.approot+'/arch')
        else:
            raise HTTPRedirectPost(req.app.approot+'/arch/'+dirname)
        
    if not dirname:
        req._dirname = ''
        return han(req)
    
    pathname = os.path.join(req.app.archive_dir, dirname)
    try:
        pathname = os.path.realpath(pathname, strict=True)
    except:
        msg = 'Directory not found: %s' % (dirname,)
        raise HTTPError('404 Not Found', msg)
    if (not os.path.isdir(pathname)) or os.path.islink(pathname):
        msg = 'Not a directory: %s' % (dirname,)
        raise HTTPError('404 Not Found', msg)
    if not pathname.startswith(req.app.archive_dir):
        msg = 'Not an Archive directory: %s' % (dirname,)
        raise HTTPError('404 Not Found', msg)
    val = pathname[ len(req.app.archive_dir) : ]
    if val.startswith('/'):
        val = val[ 1 : ]
    req._dirname = val
    req.loginfo('### _dirname=%s', val)
    return han(req)

@beforeall(require_user)
@beforeall(check_archive_dir)
class han_ArchiveDir(base_DirectoryPage):
    renderparams = {
        'navtab': 'archive',
        'filebuttons': None, ###
    }
    template = 'archivedir.html'

    def add_renderparams(self, req, map):
        if not req._dirname:
            map['uribase'] = 'arch'
            map['dirname'] = ''
        else:
            map['uribase'] = 'arch/' + req._dirname
            map['dirname'] = req._dirname
        map['files'] = self.get_filelist(req)
        return map

    def get_dirpath(self, req):
        if not req._dirname:
            return self.app.archive_dir
        else:
            return os.path.join(self.app.archive_dir, req._dirname)


@beforeall(require_role('incoming', 'admin'))
class han_UploadLog(AdminHandler):
    renderparams = { 'navtab':'uploads', 'uribase':'uploadlog' }

    PAGE_LIMIT = 20

    def do_get(self, req):
        val = req.get_query_field('start')
        if val:
            start = int(val)
        else:
            start = 0
        curs = self.app.getdb().cursor()
        res = curs.execute('SELECT * FROM uploads ORDER BY uploadtime DESC LIMIT ? OFFSET ?', (self.PAGE_LIMIT, start,))
        uploads = [ UploadEntry(tup, user=req._user) for tup in res.fetchall() ]
        return self.render('uploadlog.html', req, uploads=uploads, start=start, limit=self.PAGE_LIMIT, prevstart=max(0, start-self.PAGE_LIMIT), nextstart=start+self.PAGE_LIMIT)
    
        
class han_DebugDump(AdminHandler):
    """Display all request information. I used this a lot during testing
    but it should be disabled in production.
    """
    def do_get(self, req):
        req.set_content_type(PLAINTEXT)
        yield 'sys.version: %s\n' % (sys.version,)
        yield 'sys.path: %s\n' % (sys.path,)
        if req.match:
            yield 'match: %s\n' % (req.match,)
            yield 'match.groups: %s\n' % (req.match.groups(),)
            yield 'match.groupdict: %s\n' % (req.match.groupdict(),)
        yield 'getpid=%s\n' % (os.getpid(),)
        yield 'getuid=%s, geteuid=%s, getgid=%s, getegid=%s\n' % (os.getuid(), os.geteuid(), os.getgid(), os.getegid(),)
        yield 'environ:\n'
        for key, val in req.env.items():
            yield '  %s: %s\n' % (key, val,)
        if req.query:
            yield 'query: %s\n' % (req.query,)
        if 'wsgi.input' in req.env:
            val = req.env['wsgi.input'].read()
            yield 'input: %s' % (val,)

# Create the master handler list.
handlers = [
    ('', han_Home),
    ('/logout', han_LogOut),
    ('/user', han_UserProfile),
    ('/user/changepw', han_ChangePW),
    ('/user/changetz', han_ChangeTZ),
    ('/admin', han_AdminAdmin),
    ('/admin/allusers', han_AllUsers),
    ('/admin/allsessions', han_AllSessions),
    ('/incoming', han_Incoming),
    ('/trash', han_Trash),
    ('/arch/unprocessed', han_Unprocessed),
    ('/arch/(?P<dir>.+)', han_ArchiveDir),
    ('/uploadlog', han_UploadLog),
    ('/debugdump', han_DebugDump),
    ('/debugdump/(?P<arg>.+)', han_DebugDump),
]

# Create the application instance itself.
appinstance = AdminApp(config, handlers)

# Set up the WSGI entry point.
application = appinstance.application



if __name__ == '__main__':
    import adminlib.cli
    adminlib.cli.run(appinstance)
