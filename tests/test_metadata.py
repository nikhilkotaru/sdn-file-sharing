"""
Upload-lock tests for the metadata store (no network needed).

A client whose upload timed out retries on a new connection while the server
may still be draining the old one. The retry must not be refused, and the
stale attempt must not be able to overwrite the newer one.
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from metadata import MetadataStore, MetadataError


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


class UploadLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MetadataStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_user_retry_takes_over(self):
        old = self.store.begin_upload("big.bin", "alice", "user")
        new = self.store.begin_upload("big.bin", "alice", "user")      # retry: not refused
        write(new, b"new")
        self.store.commit_upload("big.bin", new, 3, "x", "alice")
        self.assertEqual(self.store.get("big.bin")["size"], 3)
        write(old, b"stale-data")
        with self.assertRaises(MetadataError):                         # stale attempt cannot commit
            self.store.commit_upload("big.bin", old, 10, "y", "alice")
        with open(self.store.path("big.bin"), "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_stale_abort_keeps_newer_lock(self):
        old = self.store.begin_upload("big.bin", "alice", "user")
        new = self.store.begin_upload("big.bin", "alice", "user")
        self.store.abort_upload("big.bin", old)                        # old connection finally dies
        with self.assertRaises(MetadataError):                         # newer upload still protected
            self.store.begin_upload("big.bin", "bob", "user")
        self.store.abort_upload("big.bin", new)
        self.store.begin_upload("big.bin", "bob", "user")              # lock released

    def test_other_user_still_refused(self):
        self.store.begin_upload("big.bin", "alice", "user")
        with self.assertRaises(MetadataError):
            self.store.begin_upload("big.bin", "bob", "user")


if __name__ == "__main__":
    unittest.main()
