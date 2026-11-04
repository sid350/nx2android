#!/usr/bin/env python3
# The dropbox.txt of cloud saves (docs/SAVE_SYNC.md): OAuth PKCE for the player's own Dropbox app, no app secret. The token is written to the file and never printed.
# Usage: dropbox_token.py <App key> [output file]
import base64, hashlib, json, os, secrets, sys, urllib.error, urllib.parse, urllib.request

SCOPES = 'files.metadata.read files.content.read files.content.write'


def main():
    if len(sys.argv) < 2: sys.exit('usage: dropbox_token.py <App key> [output file]')
    key, out = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'dropbox.txt'
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    print('Open this link, sign in and allow the app:\n')
    print('https://www.dropbox.com/oauth2/authorize?' + urllib.parse.urlencode({'client_id': key, 'response_type': 'code', 'token_access_type': 'offline', 'code_challenge': challenge,
                                                                                'code_challenge_method': 'S256', 'scope': SCOPES}))
    code = input('\nThe code Dropbox shows: ').strip()
    body = urllib.parse.urlencode({'code': code, 'grant_type': 'authorization_code', 'client_id': key, 'code_verifier': verifier}).encode()
    try: answer = json.load(urllib.request.urlopen(urllib.request.Request('https://api.dropboxapi.com/oauth2/token', body), timeout=30))
    except urllib.error.HTTPError as e: sys.exit(f'Dropbox refused the code (HTTP {e.code}): the code is good for one try, run this again')
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', newline='\n') as f: f.write(f"app_key={key}\nrefresh_token={answer['refresh_token']}\n")
    print(f'{out}: written. Keep it private; see docs/SAVE_SYNC.md for where it goes.')


if __name__ == '__main__':
    main()
