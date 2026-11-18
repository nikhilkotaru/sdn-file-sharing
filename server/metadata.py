"""
Thread-safe file metadata store.

Uploads are written to a temporary file and atomically renamed on commit,
so concurrent downloads always see either the old or the new complete file.
Only one upload per file name may be in progress at a time. If the same user
starts a new upload of a name while an older one is still in progress (typically
a client retry whose previous connection the server has not yet noticed is dead),
the new upload takes over and the stale one can no longer commit.
"""
import json
import os
import re
import threading
import time
import uuid

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MetadataError(Exception):
    pass


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name))


class MetadataStore:
    def __init__(self, storage_dir):
        self.dir = os.path.abspath(storage_dir)
        self.files_dir = os.path.join(self.dir, "files")
        self.tmp_dir = os.path.join(self.dir, "tmp")
        self.meta_path = os.path.join(self.dir, "metadata.json")
        os.makedirs(self.files_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.uploading = {}            # name -> (user, tmp_path of the current attempt)
        self.meta = {}
        if os.path.exists(self.meta_path):
            with open(self.meta_path) as f:
                self.meta = json.load(f)
        # drop records whose file vanished
        self.meta = {n: m for n, m in self.meta.items() if os.path.exists(self.path(n))}
        for leftover in os.listdir(self.tmp_dir):
            os.remove(os.path.join(self.tmp_dir, leftover))

    def path(self, name):
        return os.path.join(self.files_dir, name)

    def _save(self):
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.meta, f, indent=2)
        os.replace(tmp, self.meta_path)

    # ------------------------------------------------------------ queries
    def list(self):
        with self.lock:
            return [dict(name=n, **m) for n, m in sorted(self.meta.items())]

    def get(self, name):
        with self.lock:
            m = self.meta.get(name)
            return dict(name=name, **m) if m else None

    # ------------------------------------------------------------ uploads
    def begin_upload(self, name, user, role):
        if not valid_name(name):
            raise MetadataError("invalid file name")
        with self.lock:
            current = self.uploading.get(name)
            if current and current[0] != user:
                raise MetadataError("another upload of this file is in progress")
            existing = self.meta.get(name)
            if existing and existing["owner"] != user and role != "admin":
                raise MetadataError("file exists and is owned by %s" % existing["owner"])
            tmp = os.path.join(self.tmp_dir, "%s.%s.part" % (name, uuid.uuid4().hex))
            self.uploading[name] = (user, tmp)     # same user: newest attempt takes over
        return tmp

    def _is_current(self, name, tmp_path):
        cur = self.uploading.get(name)
        return cur is not None and cur[1] == tmp_path

    def commit_upload(self, name, tmp_path, size, sha256, user):
        with self.lock:
            if not self._is_current(name, tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise MetadataError("upload superseded by a newer attempt")
            try:
                os.replace(tmp_path, self.path(name))
                old = self.meta.get(name, {})
                self.meta[name] = {
                    "size": size, "sha256": sha256, "owner": old.get("owner", user),
                    "uploaded_by": user, "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "version": old.get("version", 0) + 1, "downloads": old.get("downloads", 0),
                }
                self._save()
            finally:
                self.uploading.pop(name, None)

    def abort_upload(self, name, tmp_path):
        with self.lock:
            if self._is_current(name, tmp_path):   # never cancel a newer attempt
                self.uploading.pop(name, None)
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ------------------------------------------------------------ others
    def record_download(self, name):
        with self.lock:
            if name in self.meta:
                self.meta[name]["downloads"] += 1
                self._save()

    def delete(self, name, user, role):
        with self.lock:
            m = self.meta.get(name)
            if not m:
                raise MetadataError("no such file")
            if m["owner"] != user and role != "admin":
                raise MetadataError("permission denied: owned by %s" % m["owner"])
            if name in self.uploading:
                raise MetadataError("file is being uploaded")
            os.remove(self.path(name))
            del self.meta[name]
            self._save()
