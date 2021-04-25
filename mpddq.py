#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import anyio
import mpd.asyncio
import random
import argparse
import yaml
import contextlib
import dataclasses
import copy

try:
    import asyncinotify
except OSError:
    print("Warning: asyncinotify not available")
    asyncinotify = None


def get_config_defaults():
    return {
        "host": "localhost",
        "port": 6600,
        "partitions": {"default": {}},
    }


def get_partition_config_defaults():
    return {
        "enabled": True,
        "min-len": 10,
        "max-hist-len": float('inf'),
        "clear-when-stopped": False,
        "source-playlists": {},
    }


class BigConfigChange(Exception): pass
class PartitionOnlyConfigChange(Exception): pass


class Mpddq:
    def __init__(self, config):
        self.conf_filename = config
        self.stored_playlists = {}
        self.config = None

    async def load_config(self):
        self.config = await anyio.to_thread.run_sync(self._sync_load_config, self.conf_filename)

    @classmethod
    def _sync_load_config(cls, conf_filename):
        try:
            with open(conf_filename, 'r') as f:
                raw_params = yaml.safe_load(f)
        except FileNotFoundError:
            raw_params = {}
        raw_params_copy = copy.deepcopy(raw_params)

        config = get_config_defaults()
        for k in config.keys():
            try:
                config[k] = raw_params[k]
            except KeyError:
                pass
        partitions_section = config["partitions"]
        for part_name, part_params in partitions_section.items():
            partitions_section[part_name] = get_partition_config_defaults()
            for k in partitions_section[part_name].keys():
                try:
                    partitions_section[part_name][k] = part_params[k]
                except KeyError:
                    pass

        if config != raw_params_copy:
            cls._sync_store_config(conf_filename, config)
        return config

    @classmethod
    def _sync_store_config(cls, conf_filename, config):
        with open(conf_filename, 'w') as f:
            yaml.safe_dump(config, f, indent=4, default_flow_style=False, sort_keys=False)

    async def run(self):
        await self.load_config()

        while True:
            with contextlib.suppress(BigConfigChange):
                async with self.connect() as self.mpd:
                    print("Connected")

                    await self.load_stored_playlists()

                    while True:
                        with contextlib.suppress(PartitionOnlyConfigChange):
                            async with anyio.create_task_group() as tasks:
                                for name, config in self.config["partitions"].items():
                                    tasks.start_soon(PartitionMonitor(
                                        connect=self.connect,
                                        name=name,
                                        config=config,
                                        stored_playlists=self.stored_playlists,
                                    ).run)
                                tasks.start_soon(self.keep_updating_stored_playlists)
                                tasks.start_soon(self.keep_watching_config_file)

    @contextlib.asynccontextmanager
    async def connect(self):
        client = mpd.asyncio.MPDClient()
        await client.connect(self.config["host"], self.config["port"])
        try:
            yield client
        finally:
            client.disconnect()

    async def load_stored_playlists(self):
        playlist_list = await self.mpd.listplaylists()
        for pl in playlist_list:
            name = pl["playlist"]
            last_modified = pl["last-modified"]
            if name not in self.stored_playlists or self.stored_playlists[name].last_modified != last_modified:
                print(f"Loading contents of playlist {repr(name)}")
                self.stored_playlists[name] = StoredPlaylist(
                    last_modified=last_modified,
                    tracks=list(await self.mpd.listplaylist(name))
                )

    async def keep_updating_stored_playlists(self):
        async with contextlib.aclosing(self.mpd.idle((
            "stored_playlist",
        ))) as iter_subsystems:
            async for subsystem in iter_subsystems:
                await self.load_stored_playlists()

    async def keep_watching_config_file(self):
        if asyncinotify is None:
            return
        with asyncinotify.Inotify() as inotify:
            inotify.add_watch(self.conf_filename, asyncinotify.Mask.MODIFY)
            async for event in inotify:
                print(event)
                prev_config = self.config
                await self.load_config()
                if self.config != prev_config:
                    print("Config file was changed")
                    params_diff = {k: v for k, v in self.config.items() if v != prev_config[k]}
                    if list(params_diff.keys()) == ["partitions"]:
                        raise PartitionOnlyConfigChange
                    else:
                        raise BigConfigChange


@dataclasses.dataclass
class StoredPlaylist:
    last_modified: str
    tracks: list[str]


class PartitionMonitor:
    def __init__(self, connect, name, config, stored_playlists):
        self.connect = connect
        self.name = name
        self.config = config
        self.stored_playlists = stored_playlists
        self.playlist_picker = self.make_stored_playlist_picker()

    async def run(self):
        if not self.config["enabled"] or self.playlist_picker is None:
            return

        async with self.connect() as self.mpd:
            await self.enter_partition()
            self.status = self.prev_status = await self.mpd.status()
            print(f"{self.name}: Connected")

            await self.update_queue()
            async with contextlib.aclosing(self.mpd.idle((
                "player",
                "playlist",
                "options",
            ))) as iter_subsystems:
                async for subsystem in iter_subsystems:
                    self.prev_status = self.status
                    self.status = await self.mpd.status()
                    await self.update_queue()

    async def enter_partition(self):
        for _ in range(3):
            try:
                await self.mpd.partition(self.name)
                return
            except mpd.CommandError as e:
                if e.errno != mpd.FailureResponseCode.NO_EXIST:
                    raise
            try:
                await self.mpd.newpartition(self.name)
            except mpd.CommandError as e:
                if e.errno != mpd.FailureResponseCode.EXIST:
                    raise
        raise RuntimeError(f"Failed to create partition {self.name}")

    async def update_queue(self):
        if await self.is_dynamic_enabled():
            await self.remove_old_tracks()
            await self.add_new_tracks()

    async def is_dynamic_enabled(self):
        try:
            if int(self.status["random"]):
                return False
            if int(self.status["repeat"]) and not int(self.status["single"]):
                return False
        except KeyError as e:
            print(f"{self.name}: Warning! MPD status has no key {repr(e.args[0])}")
            return False
        return True

    async def remove_old_tracks(self):
        if self.config["clear-when-stopped"]:
            if self.status["state"]=="stop" and self.prev_status["state"]!="stop":
                print(f"{self.name}: Clearing the queue because playback was stopped")
                await self.mpd.clear()
                return
        try:
            current_track = int(self.status['song'])
        except KeyError:
            pass
        else:
            n_tracks_to_remove = current_track - self.config['max-hist-len']
            if n_tracks_to_remove > 0:
                print(f"{self.name}: Removing {n_tracks_to_remove} track{'' if n_tracks_to_remove==1 else 's'}")
                await self.mpd.delete((0,n_tracks_to_remove))

    def make_stored_playlist_picker(self):
        src = self.config["source-playlists"]
        if not src:
            playlist_picker = None
        elif isinstance(src, str):
            def playlist_picker():
                return src
        elif isinstance(src, list):
            def playlist_picker():
                return random.choice(src)
        else:
            k = list(src.keys())
            w = list(src.values())
            def playlist_picker():
                result, = random.choices(k, weights=w)
                return result
        return playlist_picker

    async def add_new_tracks(self):
        async def is_too_short():
            current_length = int((await self.mpd.status())['playlistlength'])
            return current_length < self.config['min-len']

        async def choose_new_track():
            while True:
                pl_name = self.playlist_picker()
                try:
                    return random.choice(self.stored_playlists[pl_name].tracks)
                except KeyError:
                    print(f"{self.name}: Warning! Playlist {repr(pl_name)} doesn't exist")
                except IndexError:
                    print(f"{self.name}: Warning! Playlist {repr(pl_name)} is empty")
                await anyio.sleep(5)

        while await is_too_short():
            new_track = await choose_new_track()
            print(f"{self.name}: Adding {new_track}")
            await self.mpd.add(new_track)


if not hasattr(contextlib, "aclosing"):
    class aclosing:
        def __init__(self, thing):
            self.thing = thing
        async def __aenter__(self):
            return self.thing
        async def __aexit__(self, *exc_info):
            await self.thing.aclose()
    contextlib.aclosing = aclosing
    del aclosing


def startDaemon(config):
    anyio.run(Mpddq(config).run, backend='asyncio')


def main():
    parser = argparse.ArgumentParser(prog='mpddq')
    subparsers = parser.add_subparsers()

    daemon = subparsers.add_parser('daemon')
    daemon.set_defaults(func=startDaemon)
    daemon.add_argument("--config", required=True, type=pathlib.Path, help="Path to the config file")

    args = parser.parse_args().__dict__
    try:
        func = args.pop('func')
    except KeyError:
        parser.print_help()
        raise SystemExit(1)

    func(**args)


if __name__ == "__main__":
    main()

