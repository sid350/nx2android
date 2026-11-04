# Cloud saves over Dropbox

One game's save can follow the player between Android, a Mac and an iPhone: `dm/host/save_sync.c` (the same code and the same `save.dmsync` format as nxnative) packs the committed save, compares hashes and decides which way it goes. On Android the cloud's folder is a folder in the player's Dropbox (`dm/android/save_sync_android.c`). It is off until the player sets it up, and every app is set up on its own.

## Setting it up

1. Make your own Dropbox app at dropbox.com/developers/apps: Scoped access, App folder. On Permissions tick `files.metadata.read`, `files.content.read` and `files.content.write`. Copy the App key from Settings. No app secret is used.
2. On a computer, run `python devtools/dropbox_token.py <App key>`. Sign in at the link it prints and paste back the code Dropbox shows. It writes `dropbox.txt` in the current folder. The file holds a token that stands for your account in that app's folder: keep it out of git and do not share it.
3. Put that file in the app's files folder, `/sdcard/Android/data/<package>/files/`:

```
adb push dropbox.txt /sdcard/Android/data/<package>/files/dropbox.txt
```

The file has these lines (`docs/dropbox.txt.example`):

```
app_key=
refresh_token=
```

At its next start the app moves the file to its private folder (`no_backup/dropbox.conf`, out of Android's backups) and removes it from the files folder, which other apps and a computer can read. To change the settings, put a new `dropbox.txt` there. `DM_SAVE_SYNC=0` in `env.txt` turns sync off.

The saves are `/Saves/<title ID>/save.dmsync` from the root of the Dropbox app's folder; `folder=` in the file names another folder (no spaces).

**On an iPhone or a Mac,** choose the same folder in the app's Cloud saves: `Apps/<your app's name>/Saves` (`Приложения/...` in a Russian Dropbox). If a plain folder of that name already exists when you make the Dropbox app, Dropbox names the app's folder differently (`NXNative (1)`); check which folder has the puzzle icon, and that it is the one every device uses.

## How it behaves

**When.** The cloud's save comes in at the game's first save mount, waiting at most 10 seconds; without an answer the game plays on with the save it has. A save goes out 3 seconds after the game's last commit, and when the app goes to the background. Android may freeze an app soon after that, so a save made just before leaving may go out only at the next start.

**Which way.** Only one side changed: that one wins. Both changed: the newer one wins. Every save replaced is kept first in `<save>-backups/` inside the app's private data (the newest 10 of each side). Export save and Import save work as before: an imported save counts as changed here, and goes out.

**What to watch for.**

- If the cloud's save does not arrive within those 10 seconds, the game runs with the older save it has. Saving in that run sends the older save over the newer one; the newer one is kept only in this device's backups. Seen once on an iPhone, whose download goes through the Dropbox app. If the game shows old progress, close it without saving and start it again.
- Deleting part of a save counts as a change, and it goes out.
- Only one device should play at a time: a game left open on another device can send its save over this one.

The log (`log.txt`) has what happened, on the lines starting `save sync:`. The tokens are never written there.
