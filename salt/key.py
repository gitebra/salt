# -*- coding: utf-8 -*-
'''
The Salt Key backend API and interface used by the CLI. The Key class can be
used to manage salt keys directly without interfacing with the CLI.
'''

# Import python libs
from __future__ import absolute_import, print_function
import os
import copy
import json
import stat
import shutil
import fnmatch
import hashlib
import logging

# Import salt libs
import salt.cache
import salt.client
import salt.crypt
import salt.daemons.masterapi
import salt.exceptions
import salt.minion
import salt.utils
import salt.utils.event
import salt.utils.kinds

# pylint: disable=import-error,no-name-in-module,redefined-builtin
import salt.ext.six as six
from salt.ext.six.moves import input
# pylint: enable=import-error,no-name-in-module,redefined-builtin

# Import third party libs
try:
    import msgpack
except ImportError:
    pass

log = logging.getLogger(__name__)


def get_key(opts):
    if opts['transport'] in ('zeromq', 'tcp'):
        return Key(opts)
    else:
        return RaetKey(opts)


class KeyCLI(object):
    '''
    Manage key CLI operations
    '''
    CLI_KEY_MAP = {'list': 'list_status',
                   'delete': 'delete_key',
                   'gen_signature': 'gen_keys_signature',
                   'print': 'key_str',
                   }

    def __init__(self, opts):
        self.opts = opts
        self.client = salt.wheel.WheelClient(opts)
        if self.opts['transport'] in ('zeromq', 'tcp'):
            self.key = Key
        else:
            self.key = RaetKey
        # instantiate the key object for masterless mode
        if not opts.get('eauth'):
            self.key = self.key(opts)
        self.auth = None

    def _update_opts(self):
        # get the key command
        for cmd in ('gen_keys',
                    'gen_signature',
                    'list',
                    'list_all',
                    'print',
                    'print_all',
                    'accept',
                    'accept_all',
                    'reject',
                    'reject_all',
                    'delete',
                    'delete_all',
                    'finger',
                    'finger_all',
                    'list_all'):  # last is default
            if self.opts[cmd]:
                break
        # set match if needed
        if not cmd.startswith('gen_'):
            if cmd == 'list_all':
                self.opts['match'] = 'all'
            elif cmd.endswith('_all'):
                self.opts['match'] = '*'
            else:
                self.opts['match'] = self.opts[cmd]
            if cmd.startswith('accept'):
                self.opts['include_rejected'] = self.opts['include_all'] or self.opts['include_rejected']
                self.opts['include_accepted'] = False
            elif cmd.startswith('reject'):
                self.opts['include_accepted'] = self.opts['include_all'] or self.opts['include_accepted']
                self.opts['include_rejected'] = False
        elif cmd == 'gen_keys':
            self.opts['keydir'] = self.opts['gen_keys_dir']
            self.opts['keyname'] = self.opts['gen_keys']
        # match is set to opts, now we can forget about *_all commands
        self.opts['fun'] = cmd.replace('_all', '')

    def _init_auth(self):
        if self.auth:
            return

        low = {}
        skip_perm_errors = self.opts['eauth'] != ''

        if self.opts['eauth']:
            if 'token' in self.opts:
                try:
                    with salt.utils.fopen(os.path.join(self.opts['cachedir'], '.root_key'), 'r') as fp_:
                        low['key'] = fp_.readline()
                except IOError:
                    low['token'] = self.opts['token']
            #
            # If using eauth and a token hasn't already been loaded into
            # low, prompt the user to enter auth credentials
            if 'token' not in low and 'key' not in low and self.opts['eauth']:
                # This is expensive. Don't do it unless we need to.
                resolver = salt.auth.Resolver(self.opts)
                res = resolver.cli(self.opts['eauth'])
                if self.opts['mktoken'] and res:
                    tok = resolver.token_cli(
                            self.opts['eauth'],
                            res
                            )
                    if tok:
                        low['token'] = tok.get('token', '')
                if not res:
                    log.error('Authentication failed')
                    return {}
                low.update(res)
                low['eauth'] = self.opts['eauth']
        else:
            low['user'] = salt.utils.get_specific_user()
            low['key'] = salt.utils.get_master_key(low['user'], self.opts, skip_perm_errors)

        self.auth = low

    def _get_args_kwargs(self, fun, args=None):
        if args is None:
            argspec = salt.utils.args.get_function_argspec(fun)
            args = []
            if argspec.args:
                for arg in argspec.args:
                    args.append(self.opts.get(arg))
        args, kwargs = salt.minion.load_args_and_kwargs(
            fun,
            args,
            self.opts,
        )
        return args, kwargs

    def _run_cmd(self, cmd, args=None):
        if not self.opts.get('eauth'):
            cmd = self.CLI_KEY_MAP.get(cmd, cmd)
            fun = getattr(self.key, cmd)
            args, kwargs = self._get_args_kwargs(fun, args)
            ret = fun(*args, **kwargs)
            if (isinstance(ret, dict) and 'local' in ret and
                        cmd not in ('finger', 'finger_all')):
                ret.pop('local', None)
            return ret

        fstr = 'key.{0}'.format(cmd)
        fun = self.client.functions[fstr]
        args, kwargs = self._get_args_kwargs(fun, args)

        low = {
                'fun': fstr,
                'arg': args,
                'kwarg': kwargs,
                }

        self._init_auth()
        low.update(self.auth)

        # Execute the key request!
        ret = self.client.cmd_sync(low)

        ret = ret['data']['return']
        if (isinstance(ret, dict) and 'local' in ret and
                cmd not in ('finger', 'finger_all')):
            ret.pop('local', None)

        return ret

    def _filter_ret(self, cmd, ret):
        if cmd.startswith('delete'):
            return ret

        keys = {}
        if self.key.PEND in ret:
            keys[self.key.PEND] = ret[self.key.PEND]
        if self.opts['include_accepted'] and bool(ret.get(self.key.ACC)):
            keys[self.key.ACC] = ret[self.key.ACC]
        if self.opts['include_rejected'] and bool(ret.get(self.key.REJ)):
            keys[self.key.REJ] = ret[self.key.REJ]
        if self.opts['include_denied'] and bool(ret.get(self.key.DEN)):
            keys[self.key.DEN] = ret[self.key.DEN]
        return keys

    def _print_no_match(self, cmd, match):
        statuses = ['unaccepted']
        if self.opts['include_accepted']:
            statuses.append('accepted')
        if self.opts['include_rejected']:
            statuses.append('rejected')
        if self.opts['include_denied']:
            statuses.append('denied')
        if len(statuses) == 1:
            stat_str = statuses[0]
        else:
            stat_str = '{0} or {1}'.format(', '.join(statuses[:-1]), statuses[-1])
        msg = 'The key glob \'{0}\' does not match any {1} keys.'.format(match, stat_str)
        print(msg)

    def run(self):
        '''
        Run the logic for saltkey
        '''
        self._update_opts()
        cmd = self.opts['fun']

        veri = None
        ret = None
        try:
            if cmd in ('accept', 'reject', 'delete'):
                ret = self._run_cmd('name_match')
                if not isinstance(ret, dict):
                    salt.output.display_output(ret, 'key', opts=self.opts)
                    return ret
                ret = self._filter_ret(cmd, ret)
                if not ret:
                    self._print_no_match(cmd, self.opts['match'])
                    return
                print('The following keys are going to be {0}ed:'.format(cmd))
                salt.output.display_output(ret, 'key', opts=self.opts)

                if not self.opts.get('yes', False):
                    try:
                        if cmd.startswith('delete'):
                            veri = input('Proceed? [N/y] ')
                            if not veri:
                                veri = 'n'
                        else:
                            veri = input('Proceed? [n/Y] ')
                            if not veri:
                                veri = 'y'
                    except KeyboardInterrupt:
                        raise SystemExit("\nExiting on CTRL-c")

            if veri is None or veri.lower().startswith('y'):
                ret = self._run_cmd(cmd)
                if isinstance(ret, dict):
                    salt.output.display_output(ret, 'key', opts=self.opts)
                else:
                    salt.output.display_output({'return': ret}, 'key', opts=self.opts)
        except salt.exceptions.SaltException as exc:
            ret = '{0}'.format(exc)
            if not self.opts.get('quiet', False):
                salt.output.display_output(ret, 'nested', self.opts)
        return ret


class MultiKeyCLI(KeyCLI):
    '''
    Manage multiple key backends from the CLI
    '''
    def __init__(self, opts):
        opts['__multi_key'] = True
        super(MultiKeyCLI, self).__init__(opts)
        # Remove the key attribute set in KeyCLI.__init__
        delattr(self, 'key')
        zopts = copy.copy(opts)
        ropts = copy.copy(opts)
        self.keys = {}
        zopts['transport'] = 'zeromq'
        self.keys['ZMQ Keys'] = KeyCLI(zopts)
        ropts['transport'] = 'raet'
        self.keys['RAET Keys'] = KeyCLI(ropts)

    def _call_all(self, fun, *args):
        '''
        Call the given function on all backend keys
        '''
        for kback in self.keys:
            print(kback)
            getattr(self.keys[kback], fun)(*args)

    def list_status(self, status):
        self._call_all('list_status', status)

    def list_all(self):
        self._call_all('list_all')

    def accept(self, match, include_rejected=False, include_denied=False):
        self._call_all('accept', match, include_rejected, include_denied)

    def accept_all(self, include_rejected=False, include_denied=False):
        self._call_all('accept_all', include_rejected, include_denied)

    def delete(self, match):
        self._call_all('delete', match)

    def delete_all(self):
        self._call_all('delete_all')

    def reject(self, match, include_accepted=False, include_denied=False):
        self._call_all('reject', match, include_accepted, include_denied)

    def reject_all(self, include_accepted=False, include_denied=False):
        self._call_all('reject_all', include_accepted, include_denied)

    def print_key(self, match):
        self._call_all('print_key', match)

    def print_all(self):
        self._call_all('print_all')

    def finger(self, match):
        self._call_all('finger', match)

    def finger_all(self):
        self._call_all('finger_all')

    def prep_signature(self):
        self._call_all('prep_signature')


class Key(object):
    '''
    The object that encapsulates saltkey actions
    '''
    ACC = 'minions'
    PEND = 'minions_pre'
    REJ = 'minions_rejected'
    DEN = 'minions_denied'

    def __init__(self, opts):
        self.opts = opts
        kind = self.opts.get('__role', '')  # application kind
        if kind not in salt.utils.kinds.APPL_KINDS:
            emsg = ("Invalid application kind = '{0}'.".format(kind))
            log.error(emsg + '\n')
            raise ValueError(emsg)
        self.event = salt.utils.event.get_event(
                kind,
                opts['sock_dir'],
                opts['transport'],
                opts=opts,
                listen=False)

    def _check_minions_directories(self):
        '''
        Return the minion keys directory paths
        '''
        minions_accepted = os.path.join(self.opts['pki_dir'], self.ACC)
        minions_pre = os.path.join(self.opts['pki_dir'], self.PEND)
        minions_rejected = os.path.join(self.opts['pki_dir'],
                                        self.REJ)

        minions_denied = os.path.join(self.opts['pki_dir'],
                                        self.DEN)
        return minions_accepted, minions_pre, minions_rejected, minions_denied

    def _get_key_attrs(self, keydir, keyname,
                       keysize, user):
        if not keydir:
            if 'gen_keys_dir' in self.opts:
                keydir = self.opts['gen_keys_dir']
            else:
                keydir = self.opts['pki_dir']
        if not keyname:
            if 'gen_keys' in self.opts:
                keyname = self.opts['gen_keys']
            else:
                keyname = 'minion'
        if not keysize:
            keysize = self.opts['keysize']
        return keydir, keyname, keysize, user

    def gen_keys(self, keydir=None, keyname=None, keysize=None, user=None):
        '''
        Generate minion RSA public keypair
        '''
        keydir, keyname, keysize, user = self._get_key_attrs(keydir, keyname,
                                                             keysize, user)
        salt.crypt.gen_keys(keydir, keyname, keysize, user)
        return salt.utils.pem_finger(os.path.join(keydir, keyname + '.pub'))

    def gen_signature(self, privkey, pubkey, sig_path):
        '''
        Generate master public-key-signature
        '''
        return salt.crypt.gen_signature(privkey,
                                        pubkey,
                                        sig_path)

    def gen_keys_signature(self, priv, pub, signature_path, auto_create=False, keysize=None):
        '''
        Generate master public-key-signature
        '''
        # check given pub-key
        if pub:
            if not os.path.isfile(pub):
                return 'Public-key {0} does not exist'.format(pub)
        # default to master.pub
        else:
            mpub = self.opts['pki_dir'] + '/' + 'master.pub'
            if os.path.isfile(mpub):
                pub = mpub

        # check given priv-key
        if priv:
            if not os.path.isfile(priv):
                return 'Private-key {0} does not exist'.format(priv)
        # default to master_sign.pem
        else:
            mpriv = self.opts['pki_dir'] + '/' + 'master_sign.pem'
            if os.path.isfile(mpriv):
                priv = mpriv

        if not priv:
            if auto_create:
                log.debug('Generating new signing key-pair {0}.* in {1}'
                      ''.format(self.opts['master_sign_key_name'],
                                self.opts['pki_dir']))
                salt.crypt.gen_keys(self.opts['pki_dir'],
                                    self.opts['master_sign_key_name'],
                                    keysize or self.opts['keysize'],
                                    self.opts.get('user'))

                priv = self.opts['pki_dir'] + '/' + self.opts['master_sign_key_name'] + '.pem'
            else:
                return 'No usable private-key found'

        if not pub:
            return 'No usable public-key found'

        log.debug('Using public-key {0}'.format(pub))
        log.debug('Using private-key {0}'.format(priv))

        if signature_path:
            if not os.path.isdir(signature_path):
                log.debug('target directory {0} does not exist'
                      ''.format(signature_path))
        else:
            signature_path = self.opts['pki_dir']

        sign_path = signature_path + '/' + self.opts['master_pubkey_signature']

        skey = get_key(self.opts)
        return skey.gen_signature(priv, pub, sign_path)

    def check_minion_cache(self, preserve_minions=None):
        '''
        Check the minion cache to make sure that old minion data is cleared

        Optionally, pass in a list of minions which should have their caches
        preserved. To preserve all caches, set __opts__['preserve_minion_cache']
        '''
        if preserve_minions is None:
            preserve_minions = []
        keys = self.list_keys()
        minions = []
        for key, val in six.iteritems(keys):
            minions.extend(val)
        if not self.opts.get('preserve_minion_cache', False) or not preserve_minions:
            m_cache = os.path.join(self.opts['cachedir'], self.ACC)
            if os.path.isdir(m_cache):
                for minion in os.listdir(m_cache):
                    if minion not in minions and minion not in preserve_minions:
                        shutil.rmtree(os.path.join(m_cache, minion))
            cache = salt.cache.Cache(self.opts)
            clist = cache.list(self.ACC)
            if clist:
                for minion in cache.list(self.ACC):
                    if minion not in minions and minion not in preserve_minions:
                        cache.flush('{0}/{1}'.format(self.ACC, minion))

    def check_master(self):
        '''
        Log if the master is not running

        :rtype: bool
        :return: Whether or not the master is running
        '''
        if not os.path.exists(
                os.path.join(
                    self.opts['sock_dir'],
                    'publish_pull.ipc'
                    )
                ):
            return False
        return True

    def name_match(self, match, full=False):
        '''
        Accept a glob which to match the of a key and return the key's location
        '''
        if full:
            matches = self.all_keys()
        else:
            matches = self.list_keys()
        ret = {}
        if ',' in match and isinstance(match, str):
            match = match.split(',')
        for status, keys in six.iteritems(matches):
            for key in salt.utils.isorted(keys):
                if isinstance(match, list):
                    for match_item in match:
                        if fnmatch.fnmatch(key, match_item):
                            if status not in ret:
                                ret[status] = []
                            ret[status].append(key)
                else:
                    if fnmatch.fnmatch(key, match):
                        if status not in ret:
                            ret[status] = []
                        ret[status].append(key)
        return ret

    def dict_match(self, match_dict):
        '''
        Accept a dictionary of keys and return the current state of the
        specified keys
        '''
        ret = {}
        cur_keys = self.list_keys()
        for status, keys in six.iteritems(match_dict):
            for key in salt.utils.isorted(keys):
                for keydir in (self.ACC, self.PEND, self.REJ, self.DEN):
                    if keydir and fnmatch.filter(cur_keys.get(keydir, []), key):
                        ret.setdefault(keydir, []).append(key)
        return ret

    def local_keys(self):
        '''
        Return a dict of local keys
        '''
        ret = {'local': []}
        for fn_ in salt.utils.isorted(os.listdir(self.opts['pki_dir'])):
            if fn_.endswith('.pub') or fn_.endswith('.pem'):
                path = os.path.join(self.opts['pki_dir'], fn_)
                if os.path.isfile(path):
                    ret['local'].append(fn_)
        return ret

    def list_keys(self):
        '''
        Return a dict of managed keys and what the key status are
        '''

        key_dirs = []

        # We have to differentiate between RaetKey._check_minions_directories
        # and Zeromq-Keys. Raet-Keys only have three states while ZeroMQ-keys
        # havd an additional 'denied' state.
        key_dirs = self._check_minions_directories()

        ret = {}

        for dir_ in key_dirs:
            if dir_ is None:
                continue
            ret[os.path.basename(dir_)] = []
            try:
                for fn_ in salt.utils.isorted(os.listdir(dir_)):
                    if not fn_.startswith('.'):
                        if os.path.isfile(os.path.join(dir_, fn_)):
                            ret[os.path.basename(dir_)].append(fn_)
            except (OSError, IOError):
                # key dir kind is not created yet, just skip
                continue
        return ret

    def all_keys(self):
        '''
        Merge managed keys with local keys
        '''
        keys = self.list_keys()
        keys.update(self.local_keys())
        return keys

    def list_status(self, match):
        '''
        Return a dict of managed keys under a named status
        '''
        acc, pre, rej, den = self._check_minions_directories()
        ret = {}
        if match.startswith('acc'):
            ret[os.path.basename(acc)] = []
            for fn_ in salt.utils.isorted(os.listdir(acc)):
                if not fn_.startswith('.'):
                    if os.path.isfile(os.path.join(acc, fn_)):
                        ret[os.path.basename(acc)].append(fn_)
        elif match.startswith('pre') or match.startswith('un'):
            ret[os.path.basename(pre)] = []
            for fn_ in salt.utils.isorted(os.listdir(pre)):
                if not fn_.startswith('.'):
                    if os.path.isfile(os.path.join(pre, fn_)):
                        ret[os.path.basename(pre)].append(fn_)
        elif match.startswith('rej'):
            ret[os.path.basename(rej)] = []
            for fn_ in salt.utils.isorted(os.listdir(rej)):
                if not fn_.startswith('.'):
                    if os.path.isfile(os.path.join(rej, fn_)):
                        ret[os.path.basename(rej)].append(fn_)
        elif match.startswith('den') and den is not None:
            ret[os.path.basename(den)] = []
            for fn_ in salt.utils.isorted(os.listdir(den)):
                if not fn_.startswith('.'):
                    if os.path.isfile(os.path.join(den, fn_)):
                        ret[os.path.basename(den)].append(fn_)
        elif match.startswith('all'):
            return self.all_keys()
        return ret

    def key_str(self, match):
        '''
        Return the specified public key or keys based on a glob
        '''
        ret = {}
        for status, keys in six.iteritems(self.name_match(match)):
            ret[status] = {}
            for key in salt.utils.isorted(keys):
                path = os.path.join(self.opts['pki_dir'], status, key)
                with salt.utils.fopen(path, 'r') as fp_:
                    ret[status][key] = fp_.read()
        return ret

    def key_str_all(self):
        '''
        Return all managed key strings
        '''
        ret = {}
        for status, keys in six.iteritems(self.list_keys()):
            ret[status] = {}
            for key in salt.utils.isorted(keys):
                path = os.path.join(self.opts['pki_dir'], status, key)
                with salt.utils.fopen(path, 'r') as fp_:
                    ret[status][key] = fp_.read()
        return ret

    def accept(self, match=None, match_dict=None, include_rejected=False, include_denied=False):
        '''
        Accept public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        keydirs = [self.PEND]
        if include_rejected:
            keydirs.append(self.REJ)
        if include_denied:
            keydirs.append(self.DEN)
        for keydir in keydirs:
            for key in matches.get(keydir, []):
                try:
                    shutil.move(
                            os.path.join(
                                self.opts['pki_dir'],
                                keydir,
                                key),
                            os.path.join(
                                self.opts['pki_dir'],
                                self.ACC,
                                key)
                            )
                    eload = {'result': True,
                             'act': 'accept',
                             'id': key}
                    self.event.fire_event(eload,
                                          salt.utils.event.tagify(prefix='key'))
                except (IOError, OSError):
                    pass
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def accept_all(self):
        '''
        Accept all keys in pre
        '''
        keys = self.list_keys()
        for key in keys[self.PEND]:
            try:
                shutil.move(
                        os.path.join(
                            self.opts['pki_dir'],
                            self.PEND,
                            key),
                        os.path.join(
                            self.opts['pki_dir'],
                            self.ACC,
                            key)
                        )
                eload = {'result': True,
                         'act': 'accept',
                         'id': key}
                self.event.fire_event(eload,
                                      salt.utils.event.tagify(prefix='key'))
            except (IOError, OSError):
                pass
        return self.list_keys()

    def delete_key(self,
                    match=None,
                    match_dict=None,
                    preserve_minions=False,
                    revoke_auth=False):
        '''
        Delete public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".

        To preserve the master caches of minions who are matched, set preserve_minions
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        for status, keys in six.iteritems(matches):
            for key in keys:
                try:
                    if revoke_auth:
                        if self.opts.get('rotate_aes_key') is False:
                            print('Immediate auth revocation specified but AES key rotation not allowed. '
                                     'Minion will not be disconnected until the master AES key is rotated.')
                        else:
                            try:
                                client = salt.client.get_local_client(mopts=self.opts)
                                client.cmd_async(key, 'saltutil.revoke_auth')
                            except salt.exceptions.SaltClientError:
                                print('Cannot contact Salt master. '
                                      'Connection for {0} will remain up until '
                                      'master AES key is rotated or auth is revoked '
                                      'with \'saltutil.revoke_auth\'.'.format(key))
                    os.remove(os.path.join(self.opts['pki_dir'], status, key))
                    eload = {'result': True,
                             'act': 'delete',
                             'id': key}
                    self.event.fire_event(eload,
                                          salt.utils.event.tagify(prefix='key'))
                except (OSError, IOError):
                    pass
        if preserve_minions:
            preserve_minions_list = matches.get('minions', [])
        else:
            preserve_minions_list = []
        self.check_minion_cache(preserve_minions=preserve_minions_list)
        if self.opts.get('rotate_aes_key'):
            salt.crypt.dropfile(self.opts['cachedir'], self.opts['user'])
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def delete_den(self):
        '''
        Delete all denied keys
        '''
        keys = self.list_keys()
        for status, keys in six.iteritems(self.list_keys()):
            for key in keys[self.DEN]:
                try:
                    os.remove(os.path.join(self.opts['pki_dir'], status, key))
                    eload = {'result': True,
                                 'act': 'delete',
                                 'id': key}
                    self.event.fire_event(eload,
                                          salt.utils.event.tagify(prefix='key'))
                except (OSError, IOError):
                    pass
        self.check_minion_cache()
        return self.list_keys()

    def delete_all(self):
        '''
        Delete all keys
        '''
        for status, keys in six.iteritems(self.list_keys()):
            for key in keys:
                try:
                    os.remove(os.path.join(self.opts['pki_dir'], status, key))
                    eload = {'result': True,
                             'act': 'delete',
                             'id': key}
                    self.event.fire_event(eload,
                                          salt.utils.event.tagify(prefix='key'))
                except (OSError, IOError):
                    pass
        self.check_minion_cache()
        if self.opts.get('rotate_aes_key'):
            salt.crypt.dropfile(self.opts['cachedir'], self.opts['user'])
        return self.list_keys()

    def reject(self, match=None, match_dict=None, include_accepted=False, include_denied=False):
        '''
        Reject public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        keydirs = [self.PEND]
        if include_accepted:
            keydirs.append(self.ACC)
        if include_denied:
            keydirs.append(self.DEN)
        for keydir in keydirs:
            for key in matches.get(keydir, []):
                try:
                    shutil.move(
                            os.path.join(
                                self.opts['pki_dir'],
                                keydir,
                                key),
                            os.path.join(
                                self.opts['pki_dir'],
                                self.REJ,
                                key)
                            )
                    eload = {'result': True,
                            'act': 'reject',
                            'id': key}
                    self.event.fire_event(eload,
                                          salt.utils.event.tagify(prefix='key'))
                except (IOError, OSError):
                    pass
        self.check_minion_cache()
        if self.opts.get('rotate_aes_key'):
            salt.crypt.dropfile(self.opts['cachedir'], self.opts['user'])
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def reject_all(self):
        '''
        Reject all keys in pre
        '''
        keys = self.list_keys()
        for key in keys[self.PEND]:
            try:
                shutil.move(
                        os.path.join(
                            self.opts['pki_dir'],
                            self.PEND,
                            key),
                        os.path.join(
                            self.opts['pki_dir'],
                            self.REJ,
                            key)
                        )
                eload = {'result': True,
                         'act': 'reject',
                         'id': key}
                self.event.fire_event(eload,
                                      salt.utils.event.tagify(prefix='key'))
            except (IOError, OSError):
                pass
        self.check_minion_cache()
        if self.opts.get('rotate_aes_key'):
            salt.crypt.dropfile(self.opts['cachedir'], self.opts['user'])
        return self.list_keys()

    def finger(self, match):
        '''
        Return the fingerprint for a specified key
        '''
        matches = self.name_match(match, True)
        ret = {}
        for status, keys in six.iteritems(matches):
            ret[status] = {}
            for key in keys:
                if status == 'local':
                    path = os.path.join(self.opts['pki_dir'], key)
                else:
                    path = os.path.join(self.opts['pki_dir'], status, key)
                ret[status][key] = salt.utils.pem_finger(path, sum_type=self.opts['hash_type'])
        return ret

    def finger_all(self):
        '''
        Return fingerprints for all keys
        '''
        ret = {}
        for status, keys in six.iteritems(self.all_keys()):
            ret[status] = {}
            for key in keys:
                if status == 'local':
                    path = os.path.join(self.opts['pki_dir'], key)
                else:
                    path = os.path.join(self.opts['pki_dir'], status, key)
                ret[status][key] = salt.utils.pem_finger(path, sum_type=self.opts['hash_type'])
        return ret


class RaetKey(Key):
    '''
    Manage keys from the raet backend
    '''
    ACC = 'accepted'
    PEND = 'pending'
    REJ = 'rejected'
    DEN = None

    def __init__(self, opts):
        Key.__init__(self, opts)
        self.auto_key = salt.daemons.masterapi.AutoKey(self.opts)
        self.serial = salt.payload.Serial(self.opts)

    def _check_minions_directories(self):
        '''
        Return the minion keys directory paths
        '''
        accepted = os.path.join(self.opts['pki_dir'], self.ACC)
        pre = os.path.join(self.opts['pki_dir'], self.PEND)
        rejected = os.path.join(self.opts['pki_dir'], self.REJ)
        return accepted, pre, rejected, None

    def check_minion_cache(self, preserve_minions=False):
        '''
        Check the minion cache to make sure that old minion data is cleared
        '''
        keys = self.list_keys()
        minions = []
        for key, val in six.iteritems(keys):
            minions.extend(val)

        m_cache = os.path.join(self.opts['cachedir'], 'minions')
        if os.path.isdir(m_cache):
            for minion in os.listdir(m_cache):
                if minion not in minions:
                    shutil.rmtree(os.path.join(m_cache, minion))
            cache = salt.cache.Cache(self.opts)
            clist = cache.list(self.ACC)
            if clist:
                for minion in cache.list(self.ACC):
                    if minion not in minions and minion not in preserve_minions:
                        cache.flush('{0}/{1}'.format(self.ACC, minion))

        kind = self.opts.get('__role', '')  # application kind
        if kind not in salt.utils.kinds.APPL_KINDS:
            emsg = ("Invalid application kind = '{0}'.".format(kind))
            log.error(emsg + '\n')
            raise ValueError(emsg)
        role = self.opts.get('id', '')
        if not role:
            emsg = ("Invalid id.")
            log.error(emsg + "\n")
            raise ValueError(emsg)

        name = "{0}_{1}".format(role, kind)
        road_cache = os.path.join(self.opts['cachedir'],
                                  'raet',
                                  name,
                                  'remote')
        if os.path.isdir(road_cache):
            for road in os.listdir(road_cache):
                root, ext = os.path.splitext(road)
                if ext not in ['.json', '.msgpack']:
                    continue
                prefix, sep, name = root.partition('.')
                if not name or prefix != 'estate':
                    continue
                path = os.path.join(road_cache, road)
                with salt.utils.fopen(path, 'rb') as fp_:
                    if ext == '.json':
                        data = json.load(fp_)
                    elif ext == '.msgpack':
                        data = msgpack.load(fp_)
                    if data['role'] not in minions:
                        os.remove(path)

    def gen_keys(self, keydir=None, keyname=None, keysize=None, user=None):
        '''
        Use libnacl to generate and safely save a private key
        '''
        import libnacl.public
        d_key = libnacl.dual.DualSecret()
        keydir, keyname, _, _ = self._get_key_attrs(keydir, keyname,
                                                    keysize, user)
        path = '{0}.key'.format(os.path.join(
            keydir,
            keyname))
        d_key.save(path, 'msgpack')

    def check_master(self):
        '''
        Log if the master is not running
        NOT YET IMPLEMENTED
        '''
        return True

    def local_keys(self):
        '''
        Return a dict of local keys
        '''
        ret = {'local': []}
        fn_ = os.path.join(self.opts['pki_dir'], 'local.key')
        if os.path.isfile(fn_):
            ret['local'].append(fn_)
        return ret

    def status(self, minion_id, pub, verify):
        '''
        Accepts the minion id, device id, curve public and verify keys.
        If the key is not present, put it in pending and return "pending",
        If the key has been accepted return "accepted"
        if the key should be rejected, return "rejected"
        '''
        acc, pre, rej, _ = self._check_minions_directories()  # pylint: disable=W0632
        acc_path = os.path.join(acc, minion_id)
        pre_path = os.path.join(pre, minion_id)
        rej_path = os.path.join(rej, minion_id)
        # open mode is turned on, force accept the key
        keydata = {
                'minion_id': minion_id,
                'pub': pub,
                'verify': verify}
        if self.opts['open_mode']:  # always accept and overwrite
            with salt.utils.fopen(acc_path, 'w+b') as fp_:
                fp_.write(self.serial.dumps(keydata))
                return self.ACC
        if os.path.isfile(rej_path):
            log.debug("Rejection Reason: Keys already rejected.\n")
            return self.REJ
        elif os.path.isfile(acc_path):
            # The minion id has been accepted, verify the key strings
            with salt.utils.fopen(acc_path, 'rb') as fp_:
                keydata = self.serial.loads(fp_.read())
            if keydata['pub'] == pub and keydata['verify'] == verify:
                return self.ACC
            else:
                log.debug("Rejection Reason: Keys not match prior accepted.\n")
                return self.REJ
        elif os.path.isfile(pre_path):
            auto_reject = self.auto_key.check_autoreject(minion_id)
            auto_sign = self.auto_key.check_autosign(minion_id)
            with salt.utils.fopen(pre_path, 'rb') as fp_:
                keydata = self.serial.loads(fp_.read())
            if keydata['pub'] == pub and keydata['verify'] == verify:
                if auto_reject:
                    self.reject(minion_id)
                    log.debug("Rejection Reason: Auto reject pended.\n")
                    return self.REJ
                elif auto_sign:
                    self.accept(minion_id)
                    return self.ACC
                return self.PEND
            else:
                log.debug("Rejection Reason: Keys not match prior pended.\n")
                return self.REJ
        # This is a new key, evaluate auto accept/reject files and place
        # accordingly
        auto_reject = self.auto_key.check_autoreject(minion_id)
        auto_sign = self.auto_key.check_autosign(minion_id)
        if self.opts['auto_accept']:
            w_path = acc_path
            ret = self.ACC
        elif auto_sign:
            w_path = acc_path
            ret = self.ACC
        elif auto_reject:
            w_path = rej_path
            log.debug("Rejection Reason: Auto reject new.\n")
            ret = self.REJ
        else:
            w_path = pre_path
            ret = self.PEND
        with salt.utils.fopen(w_path, 'w+b') as fp_:
            fp_.write(self.serial.dumps(keydata))
            return ret

    def _get_key_str(self, minion_id, status):
        '''
        Return the key string in the form of:

        pub: <pub>
        verify: <verify>
        '''
        path = os.path.join(self.opts['pki_dir'], status, minion_id)
        with salt.utils.fopen(path, 'r') as fp_:
            keydata = self.serial.loads(fp_.read())
            return 'pub: {0}\nverify: {1}'.format(
                    keydata['pub'],
                    keydata['verify'])

    def _get_key_finger(self, path):
        '''
        Return a sha256 kingerprint for the key
        '''
        with salt.utils.fopen(path, 'r') as fp_:
            keydata = self.serial.loads(fp_.read())
            key = 'pub: {0}\nverify: {1}'.format(
                    keydata['pub'],
                    keydata['verify'])
        return hashlib.sha256(key).hexdigest()

    def key_str(self, match):
        '''
        Return the specified public key or keys based on a glob
        '''
        ret = {}
        for status, keys in six.iteritems(self.name_match(match)):
            ret[status] = {}
            for key in salt.utils.isorted(keys):
                ret[status][key] = self._get_key_str(key, status)
        return ret

    def key_str_all(self):
        '''
        Return all managed key strings
        '''
        ret = {}
        for status, keys in six.iteritems(self.list_keys()):
            ret[status] = {}
            for key in salt.utils.isorted(keys):
                ret[status][key] = self._get_key_str(key, status)
        return ret

    def accept(self, match=None, match_dict=None, include_rejected=False, include_denied=False):
        '''
        Accept public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        keydirs = [self.PEND]
        if include_rejected:
            keydirs.append(self.REJ)
        if include_denied:
            keydirs.append(self.DEN)
        for keydir in keydirs:
            for key in matches.get(keydir, []):
                try:
                    shutil.move(
                            os.path.join(
                                self.opts['pki_dir'],
                                keydir,
                                key),
                            os.path.join(
                                self.opts['pki_dir'],
                                self.ACC,
                                key)
                            )
                except (IOError, OSError):
                    pass
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def accept_all(self):
        '''
        Accept all keys in pre
        '''
        keys = self.list_keys()
        for key in keys[self.PEND]:
            try:
                shutil.move(
                        os.path.join(
                            self.opts['pki_dir'],
                            self.PEND,
                            key),
                        os.path.join(
                            self.opts['pki_dir'],
                            self.ACC,
                            key)
                        )
            except (IOError, OSError):
                pass
        return self.list_keys()

    def delete_key(self,
                   match=None,
                   match_dict=None,
                   preserve_minions=False,
                   revoke_auth=False):
        '''
        Delete public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        for status, keys in six.iteritems(matches):
            for key in keys:
                if revoke_auth:
                    if self.opts.get('rotate_aes_key') is False:
                        print('Immediate auth revocation specified but AES key rotation not allowed. '
                                 'Minion will not be disconnected until the master AES key is rotated.')
                    else:
                        try:
                            client = salt.client.get_local_client(mopts=self.opts)
                            client.cmd_async(key, 'saltutil.revoke_auth')
                        except salt.exceptions.SaltClientError:
                            print('Cannot contact Salt master. '
                                  'Connection for {0} will remain up until '
                                  'master AES key is rotated or auth is revoked '
                                  'with \'saltutil.revoke_auth\'.'.format(key))
                try:
                    os.remove(os.path.join(self.opts['pki_dir'], status, key))
                except (OSError, IOError):
                    pass
        self.check_minion_cache(preserve_minions=matches.get('minions', []))
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def delete_all(self):
        '''
        Delete all keys
        '''
        for status, keys in six.iteritems(self.list_keys()):
            for key in keys:
                try:
                    os.remove(os.path.join(self.opts['pki_dir'], status, key))
                except (OSError, IOError):
                    pass
        self.check_minion_cache()
        return self.list_keys()

    def reject(self, match=None, match_dict=None, include_accepted=False, include_denied=False):
        '''
        Reject public keys. If "match" is passed, it is evaluated as a glob.
        Pre-gathered matches can also be passed via "match_dict".
        '''
        if match is not None:
            matches = self.name_match(match)
        elif match_dict is not None and isinstance(match_dict, dict):
            matches = match_dict
        else:
            matches = {}
        keydirs = [self.PEND]
        if include_accepted:
            keydirs.append(self.ACC)
        if include_denied:
            keydirs.append(self.DEN)
        for keydir in keydirs:
            for key in matches.get(keydir, []):
                try:
                    shutil.move(
                            os.path.join(
                                self.opts['pki_dir'],
                                keydir,
                                key),
                            os.path.join(
                                self.opts['pki_dir'],
                                self.REJ,
                                key)
                            )
                except (IOError, OSError):
                    pass
        self.check_minion_cache()
        return (
            self.name_match(match) if match is not None
            else self.dict_match(matches)
        )

    def reject_all(self):
        '''
        Reject all keys in pre
        '''
        keys = self.list_keys()
        for key in keys[self.PEND]:
            try:
                shutil.move(
                        os.path.join(
                            self.opts['pki_dir'],
                            self.PEND,
                            key),
                        os.path.join(
                            self.opts['pki_dir'],
                            self.REJ,
                            key)
                        )
            except (IOError, OSError):
                pass
        self.check_minion_cache()
        return self.list_keys()

    def finger(self, match):
        '''
        Return the fingerprint for a specified key
        '''
        matches = self.name_match(match, True)
        ret = {}
        for status, keys in six.iteritems(matches):
            ret[status] = {}
            for key in keys:
                if status == 'local':
                    path = os.path.join(self.opts['pki_dir'], key)
                else:
                    path = os.path.join(self.opts['pki_dir'], status, key)
                ret[status][key] = self._get_key_finger(path)
        return ret

    def finger_all(self):
        '''
        Return fingerprints for all keys
        '''
        ret = {}
        for status, keys in six.iteritems(self.list_keys()):
            ret[status] = {}
            for key in keys:
                if status == 'local':
                    path = os.path.join(self.opts['pki_dir'], key)
                else:
                    path = os.path.join(self.opts['pki_dir'], status, key)
                ret[status][key] = self._get_key_finger(path)
        return ret

    def read_all_remote(self):
        '''
        Return a dict of all remote key data
        '''
        data = {}
        for status, mids in six.iteritems(self.list_keys()):
            for mid in mids:
                keydata = self.read_remote(mid, status)
                if keydata:
                    keydata['acceptance'] = status
                    data[mid] = keydata

        return data

    def read_remote(self, minion_id, status=ACC):
        '''
        Read in a remote key of status
        '''
        path = os.path.join(self.opts['pki_dir'], status, minion_id)
        if not os.path.isfile(path):
            return {}
        with salt.utils.fopen(path, 'rb') as fp_:
            return self.serial.loads(fp_.read())

    def read_local(self):
        '''
        Read in the local private keys, return an empy dict if the keys do not
        exist
        '''
        path = os.path.join(self.opts['pki_dir'], 'local.key')
        if not os.path.isfile(path):
            return {}
        with salt.utils.fopen(path, 'rb') as fp_:
            return self.serial.loads(fp_.read())

    def write_local(self, priv, sign):
        '''
        Write the private key and the signing key to a file on disk
        '''
        keydata = {'priv': priv,
                   'sign': sign}
        path = os.path.join(self.opts['pki_dir'], 'local.key')
        c_umask = os.umask(191)
        if os.path.exists(path):
            #mode = os.stat(path).st_mode
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        with salt.utils.fopen(path, 'w+') as fp_:
            fp_.write(self.serial.dumps(keydata))
            os.chmod(path, stat.S_IRUSR)
        os.umask(c_umask)

    def delete_local(self):
        '''
        Delete the local private key file
        '''
        path = os.path.join(self.opts['pki_dir'], 'local.key')
        if os.path.isfile(path):
            os.remove(path)

    def delete_pki_dir(self):
        '''
        Delete the private key directory
        '''
        path = self.opts['pki_dir']
        if os.path.exists(path):
            shutil.rmtree(path)
