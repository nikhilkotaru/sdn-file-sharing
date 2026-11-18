"""User store with salted PBKDF2-SHA256 password hashes and roles."""
import hashlib
import hmac
import json
import os
import threading

ITERATIONS = 100_000
ROLES = ("admin", "user", "readonly")


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    if isinstance(salt, str):
        salt = bytes.fromhex(salt)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return salt.hex(), digest.hex()


class UserStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.users = {}
        if os.path.exists(path):
            with open(path) as f:
                self.users = json.load(f)
        self._dummy_salt = os.urandom(16).hex()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.users, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, user, password, role="user"):
        if role not in ROLES:
            raise ValueError("role must be one of %s" % (ROLES,))
        salt, digest = hash_password(password)
        with self.lock:
            self.users[user] = {"salt": salt, "hash": digest, "role": role}
            self.save()

    def remove(self, user):
        with self.lock:
            self.users.pop(user, None)
            self.save()

    def verify(self, user, password):
        """Returns the user's role on success, else None (constant-ish time)."""
        rec = self.users.get(user)
        salt = rec["salt"] if rec else self._dummy_salt
        _, digest = hash_password(password or "", salt)
        if rec and hmac.compare_digest(digest, rec["hash"]):
            return rec["role"]
        return None
