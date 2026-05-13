"""
One-time script: decrypt the old encrypted Passwords.md and save as plain text.
Run with:  python3 convert_passwords.py
"""
import base64, getpass, json, os, re
from pathlib import Path

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    raise SystemExit("Run:  pip install cryptography")

HERE     = os.path.dirname(os.path.abspath(__file__))
CFG      = json.loads(Path(os.path.join(HERE, "config.json")).read_text())
ROOT     = os.path.expanduser(CFG.get("root", "~/notes/"))
PW_FILE  = os.path.join(ROOT, "Passwords", "Passwords.md")

def derive_key(master_pw: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=260_000)
    return base64.urlsafe_b64encode(kdf.derive(master_pw.encode()))

def decrypt(master_pw: str, ciphertext: str) -> str:
    raw         = base64.b64decode(ciphertext)
    salt, token = raw[:16], raw[16:]
    return Fernet(derive_key(master_pw, salt)).decrypt(token).decode()

raw = Path(PW_FILE).read_text(encoding="utf-8")
if "<!-- password-note locked -->" not in raw:
    print("File is not encrypted — nothing to do.")
    raise SystemExit(0)

blob = ""
past_marker = False
for line in raw.split("\n"):
    if past_marker:
        blob += line + "\n"
    if line.strip() == "<!-- password-note locked -->":
        past_marker = True
blob = blob.strip()

master_pw = getpass.getpass("Enter your master password: ")

try:
    plaintext = decrypt(master_pw, blob)
except InvalidToken:
    raise SystemExit("Wrong password or corrupted data.")
except Exception as e:
    raise SystemExit(f"Decryption failed: {e}")

# plaintext contains the marker + GFM table — strip the old marker, keep the table
lines = plaintext.split("\n")
table_lines = [l for l in lines if l.strip() != "<!-- password-note -->"]
table = "\n".join(table_lines).strip()

# count entries
rows = [l for l in table_lines if l.startswith("|") and "---" not in l and "Website" not in l and l.strip() != "|"]
print(f"\nDecrypted successfully — found {len(rows)} password entries.")

title = os.path.basename(PW_FILE)[:-3]
new_content = f"# {title}\n\n<!-- password-note -->\n{table}\n"
Path(PW_FILE).write_text(new_content, encoding="utf-8")
print(f"Saved plain text to: {PW_FILE}")
print("You can now delete this script.")
