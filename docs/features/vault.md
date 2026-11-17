# Family document vault

**Family → 🔐 Document vault** keeps copies of the papers you'd grab in a fire: IDs and
passports, insurance policies, deeds, prescriptions and medical notes, photos of each family
member (for "have you seen…?" posters), and contact lists. Everything is **encrypted on the
Badger's disk**. If the Badger is stolen, or someone pulls its drive, they get scrambled data.

No extra hardware is needed.

## Using it

1. **Join the family** on this phone first (Check-in board → Family, with an invite code).
2. **First time:** one family member opens the vault and chooses the **family passphrase**: at
   least 10 characters; a short sentence or four random words works well
   (`purple tractor sings loudly`). Tell the other adults.
3. The Badger then shows a **recovery key** (52 letters and digits). **Print it or write it
   down** and keep it away from the Badger: with your passports, in a safe, with a relative.
   It is shown only once.
4. **Unlock** with the passphrase. Each phone, tablet or laptop unlocks on its own. A device
   locks itself after **15 minutes** without use, when you press **Lock now**, and whenever the
   Badger restarts.
5. **Add documents:** choose files or take photos (several at once is fine, up to 25 MB each),
   pick a category, add a note ("expires 2031", "policy #…"). Or **type a note** — a contact
   list, medications and doses, allergies.
6. **Find** things by searching names and notes, or by category. **View** photos on the page;
   **Download** anything else (PDFs open in your phone's viewer).

**Forgot the passphrase?** Tap *Forgot the passphrase?*, type the recovery key and choose a new
passphrase. **Lost both?** Then the documents are gone for good — nobody, including whoever
built or administers the Badger, can open them. That is the price of a thief not being able to
either. Keep the paper originals.

**Lost a phone?** Settings → *Lock the vault on every device*, then change the passphrase.
(Also ask the Badger's admin to remove that device under Family members.)

## Backups

*Passphrase, recovery key, backup → Download encrypted backup* saves one `.tar` file of the
whole vault, still encrypted. Copy it to a USB stick or another computer. To open it you need
the family passphrase, and the small program inside the instructions:

```
python3 _vault_crypto.py list    honey-badger-vault-2026-11-17.tar
python3 _vault_crypto.py extract honey-badger-vault-2026-11-17.tar FOLDER   # writes readable copies!
python3 _vault_crypto.py restore honey-badger-vault-2026-11-17.tar /var/lib/prepper/portal/features/vault
```

`_vault_crypto.py` is in `/opt/prepper/software/portal/features/`; it needs only `python3` and
`openssl`, which Linux and macOS have. `restore` puts the encrypted files back on a (new) Badger
(run it as root, then `chown -R prepper:prepper` that folder and restart the portal).

## Who can get in — the threat model

| Who / what | What they get |
|---|---|
| **Someone steals the Badger or pulls its NVMe** | Random-looking files. They can see roughly how many documents there are and how big, and when they were changed — not names, categories, notes or contents. To read anything they must guess the passphrase; each guess costs ~0.5 s and 64 MiB on a Pi 5 (scrypt), so a long passphrase is out of reach. A short one is not: **use four or more words.** |
| **Another family member's device** | Members are trusted. A member who knows the passphrase can open everything. Each device must unlock separately; the unlocked state never leaves the Badger. |
| **Visitors on the Wi-Fi** | No access: every vault address answers *family members only*, and even a member device needs the passphrase. Wrong passphrases are limited: 5 per 10 minutes per device and per address, 20 per hour overall, then a pause of 10–15 minutes. **But** what a member sends over plain `http://` can be *read* by others: on a hotspot without a password, by anyone in range (no need to join); on a WPA2 hotspot, by anyone who knows the Wi-Fi password. So on a hotspot without a password (`[network] security = "owe"` or `"open"`) the vault refuses plain HTTP altogether and asks you to open **https://badger.lan** (install the Badger certificate first: *Phones & laptops*) or to set a Wi-Fi password. On a WPA2 hotspot it works over HTTP with a warning; https is still safer. |
| **Someone with the admin PIN but not the passphrase** | Nothing from the vault. The admin PIN is not a member key, so the vault refuses it. An admin could make themselves a member with an invite, but still needs the passphrase. They could delete the encrypted files or the whole Badger — so keep a backup. |
| **Someone with root on a running Badger while a device is unlocked** | Out of scope: they could read the portal's memory. Lock the vault when you're done; it also locks itself. |

Also: search words, names and notes are sent inside requests, never in web addresses (which
nginx writes to its log). Decrypted files are never written to disk on the Badger — they're
decrypted in memory as they're sent to your phone. What your phone does with a downloaded copy
(Files app, Photos, cloud backup) is up to you.

## How it works (for the curious)

- A random 256-bit **vault key** encrypts everything. It is stored only **wrapped**: XOR-ed with
  a pad from `scrypt(passphrase, salt, N=65536, r=8, p=1)` and sealed with HMAC-SHA256. A second
  copy is wrapped by the recovery key the same way (HMAC-SHA512 derivation). Changing the
  passphrase rewraps the key (fresh salt) — the documents aren't re-encrypted — and locks every
  other device.
- Files, notes and the **index** (names, categories, notes, who added what) are split into 1 MiB
  chunks. Each chunk is encrypted with **AES-256-CTR** by the `openssl` program, under a key and IV
  unique to that chunk, then authenticated (**encrypt-then-MAC**, HMAC-SHA256 over the blob header,
  chunk number, a last-chunk flag and the ciphertext). Tampering, swapping, reordering or cutting
  chunks is detected; a download is fully checked before the first byte is sent.
- The chunk key goes to `openssl` through a **pipe** (`-pass fd:N`), never on its command line,
  where `ps` would show it. `openssl` turns it into key‖IV with one round of PBKDF2 (the key is
  already random); the result is plain AES-256-CTR that any AES library could read.
- Why the `openssl` program and not the `cryptography` Python package: the portal is standard
  library only, `openssl` is already part of every Badger install (and of macOS, so it is tested
  on both LibreSSL and OpenSSL 3/4), and AES-GCM isn't in Python's standard library.
- The unwrapped key lives only in the portal's memory, per member device. Python can't promise
  to wipe every copy, but the key is overwritten on lock and dropped on restart.

## Limits

- 25 MB per file; 2 GB and 1000 items in all by default (`[vault]` in `prepper.toml`).
- **Swap:** if the Badger swaps to a *file or partition on the NVMe*, pieces of decrypted
  documents in memory could be paged out to disk. Raspberry Pi OS Bookworm turns on a swap file
  (`dphys-swapfile`) by default; switch it off (`sudo dphys-swapfile swapoff && sudo systemctl
  disable dphys-swapfile`) or use compressed RAM swap (zram) instead. Check with `swapon --show`.
- Sizes and counts of documents are visible on disk (not what they are).
- Restoring an older copy of the vault's files (a rollback) isn't detected; documents can't be
  changed without detection, but an old set of them can be put back.
- Nothing is uploaded anywhere. The vault never leaves the Badger unless you download a backup.
