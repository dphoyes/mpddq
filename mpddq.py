#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import os, mpd, random, argparse, argcomplete, yaml, time
from gi.repository import GLib
import functools

import dbus
import dbus.service
import _dbus_bindings as dbus_bindings
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default=True)



# Traceback (most recent call last):
#   File "/home/dphoyes/bin/mpddp", line 92, in f_with_mpd
#     return f(self, *args, **kwargs)
#   File "/home/dphoyes/bin/mpddp", line 116, in spinOnce
#     self._addNewTracks()
#   File "/home/dphoyes/bin/mpddp", line 151, in _addNewTracks
#     self.mpd.add(self._chooseNewTrack())
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 381, in mpd_command
#     return wrapper(self, name, args, callback)
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 473, in _execute
#     return retval()
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 368, in command_callback
#     res = function(self, self._read_lines())
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 311, in _parse_nothing
#     for line in lines:
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 538, in _read_lines
#     line = self._read_line()
#   File "/usr/lib/python3.7/site-packages/mpd/base.py", line 527, in _read_line
#     raise CommandError(error)
# mpd.base.CommandError: [50@0] {} No such directory



class MpddpDbus(dbus.service.Object):
    Bus_Name = 'org.MPD.Mpddp'
    Object_Path = '/org/MPD/Mpddp'
    Interface_Name = 'org.MPD.MpddpInterface'

    def __init__(self, mpddp, main_loop):
        session = dbus.SessionBus()
        if session.name_has_owner(self.Bus_Name):
            print('Mpddp daemon is already running')
            raise SystemExit(1)
        bus_name = dbus.service.BusName(self.Bus_Name, session)
        super().__init__(bus_name, self.Object_Path)
        self.mpddp = mpddp
        self.main_loop = main_loop

    @dbus.service.method(Interface_Name)
    def exit(self):
        self.main_loop.quit()

    @dbus.service.method(Interface_Name)
    def reload(self):
        self.mpddp.loadConfig()
        self.mpddp.reloadTracks()
        print("Reloaded")

    @dbus.service.method(Interface_Name, in_signature='b')
    def enable(self, on_off):
        self.mpddp.params['enabled'] = bool(on_off)
        self.mpddp.storeConfig()
        print("Enabled" if self.mpddp.params['enabled'] else "Disabled")

    @dbus.service.method(Interface_Name, in_signature='as', out_signature='b')
    def setPlaylists(self, new_playlists):
        prev_playlists = self.mpddp.params['source playlists']
        self.mpddp.params['source playlists'] = [str(p) for p in new_playlists]
        try:
            self.mpddp.reloadTracks()
        except mpd.CommandError:
            self.mpddp.params['source playlists'] = prev_playlists
            return False
        self.mpddp.storeConfig()
        return True



class Mpddp:
    def __init__(self):
        self.mpd = mpd.MPDClient()
        self.conf_filename = os.path.join(os.environ['XDG_CONFIG_HOME'], 'mpddp3')
        self.loadConfig()

    def loadConfig(self):
        try:
            with open(self.conf_filename, 'r') as f:
                self.params = yaml.safe_load(f)
        except FileNotFoundError:
            self.params = {
                'enabled': True,
                'min playlist len': 15,
                'max hist len': 4,
                'source playlists': [],
                'stored playlists': dict(),
            }
        self.storeConfig()

    def storeConfig(self):
        with open(self.conf_filename, 'w') as f:
            yaml.safe_dump(self.params, f, indent=4, default_flow_style=False)

    def connect(self):
        self.mpd.connect("localhost", 6600)
        print ("Connected")

    def _with_mpd(f):
        @functools.wraps(f)
        def f_with_mpd(self, *args, **kwargs):
            while True:
                try:
                    return f(self, *args, **kwargs)
                except mpd.ConnectionError:
                    pass

                print("Connecting")
                try:
                    self.connect()
                except (ConnectionResetError, ConnectionRefusedError):
                    pass
                time.sleep(2)
        return f_with_mpd

    @_with_mpd
    def reloadTracks(self):
        source_playlists = self.params['source playlists']
        if len(source_playlists):
            self.source_tracks = [tracks for tracks in (self.mpd.listplaylist(pl) for pl in source_playlists) if len(tracks)]
        if not len(source_playlists) or not len(self.source_tracks):
            self.source_tracks = [[tr['file'] for tr in self.mpd.listall() if 'file' in tr]]

    @_with_mpd
    def spinOnce(self):
        if self.params['enabled']:
            self._removeOldTracks()
            self._addNewTracks()
        return True

    @_with_mpd
    def mpd_lsinfo(self, path=''):
        return self.mpd.lsinfo(path)

    def all_tracks(self, path=''):
        for f in self.mpd_lsinfo(path):
            if 'directory' in f:
                yield from self.all_tracks(f['directory'])
            elif 'file' in f:
                yield f

    def _removeOldTracks(self):
        try:
            current_track = int(self.mpd.status()['song'])
            song_selected = True
        except KeyError:
            song_selected = False

        if song_selected:
            n_tracks_to_remove = current_track - self.params['max hist len']
            if n_tracks_to_remove > 0:
                self.mpd.delete((0,n_tracks_to_remove))

    def _isTooShort(self):
        current_length = int(self.mpd.status()['playlistlength'])
        return current_length < self.params['min playlist len']

    def _chooseNewTrack(self):
        return random.choice(random.choice(self.source_tracks))

    def _addNewTracks(self):
        while self._isTooShort():
            self.mpd.add(self._chooseNewTrack())

    @_with_mpd
    def generate_stored_playlists(self):
        gen = PlaylistGenerator(list(self.all_tracks()))
        for name, spec in self.params['stored playlists'].items():
            print("Generating {}".format(name))
            self.mpd.playlistclear(name)
            for t in gen(spec):
                self.mpd.playlistadd(name, t)


class PlaylistGenerator:
    import re

    def __init__(self, tracklist):
        self.tracklist = tracklist

    def gen_filter(self, attribute, pattern):
        rx = self.re.compile(pattern)
        if attribute.endswith('_not'):
            attribute = attribute[:-4]
            matches = lambda s: not rx.search(s)
        else:
            matches = rx.search

        def f(track):
            return matches(track.get(attribute, ''))

        return f

    def __call__(self, spec):
        filters = [self.gen_filter(k,v) for k,v in spec.items()]
        for t in self.tracklist:
            if all(f(t) for f in filters):
                yield t['file']


def startDaemon():
    mpddp = Mpddp()
    mpddp.reloadTracks()
    main_loop = GLib.MainLoop()
    dbus_service = MpddpDbus(mpddp, main_loop)

    GLib.timeout_add(500, mpddp.spinOnce)

    try:
        main_loop.run()
    except KeyboardInterrupt:
        raise SystemExit(0)


def generate_stored_playlists():
    mpddp = Mpddp()
    mpddp.generate_stored_playlists()
    dbusCommand('reload')


def dbusCommand(command, arg=None):
    command_args = [arg] if arg is not None else []
    session = dbus.SessionBus()
    try:
        obj = session.get_object(MpddpDbus.Bus_Name, MpddpDbus.Object_Path)
    except dbus.exceptions.DBusException:
        print('Mpddp must first be started using `mpddp daemon`')
        raise SystemExit(1)

    func = getattr(obj, command)
    res = func(*command_args, dbus_interface=MpddpDbus.Interface_Name)

    if type(res) == dbus.Boolean:
        print ("Success" if res else "Failure")
        if not res:
            raise SystemExit(1)
    elif res != None:
        print(res)

def main():
    parser = argparse.ArgumentParser(prog='mpddp')
    subparsers = parser.add_subparsers()

    subparsers.add_parser('daemon').set_defaults(func=startDaemon)
    subparsers.add_parser('regenerate').set_defaults(func=generate_stored_playlists)

    subparsers.add_parser('exit').set_defaults(func=dbusCommand, command='exit')
    subparsers.add_parser('on').set_defaults(func=dbusCommand, command='enable', arg=True)
    subparsers.add_parser('off').set_defaults(func=dbusCommand, command='enable', arg=False)

    set_playlist_parser = subparsers.add_parser('setplaylist')
    set_playlist_parser.set_defaults(func=dbusCommand, command='setPlaylists')
    set_playlist_parser.add_argument('arg', metavar='playlist', nargs='*')

    argcomplete.autocomplete(parser)
    args = parser.parse_args().__dict__
    try:
        func = args.pop('func')
    except KeyError:
        parser.print_help()
        raise SystemExit(1)

    func(**args)


if __name__ == "__main__":
    main()

