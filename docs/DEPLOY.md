# Putting the three tools on one address

This is the step-by-step guide for the server.

You run every command. Nothing here runs by itself.

Read a part all the way through before you start typing it.

Each command is written out in full, every time it is needed. You never have
to scroll back to find one.

---

## What will change and what will not

### What will change

Today the team types the server address with `:8000` on the end, and gets the
ROE–OCOD linkage tool straight away.

After this work, the same address with `:8000` shows a small page with three
cards. The team picks a tool from that page.

| Address | What appears |
|---|---|
| `/` | The chooser page, with three cards |
| `/runs` | ROE–OCOD linkage, exactly as now |
| `/donations/` | Donations reconciliation, new |
| `/psc/` | PSC reconciliation, new |

Every old link keeps working. A bookmark to `/runs/123` still opens run 123
in the ROE–OCOD tool. A link to `/labels` still opens the labels screen.

Everybody is logged out once, at the moment of the switch. They log back in
with the same password they use now. That one login then covers all three
tools. They will not be logged out again by a restart.

### What will not change

The ROE–OCOD tool's code does not change. Not one line.

Its data does not move. It stays in `/var/lib/roe_ui`.

The password does not change.

Harrier, the other app on this server, is not touched. Its own address on
port 8080 keeps working throughout, including during the switch.

Elasticsearch and Kibana are not touched.

### The one thing that is not being fixed

The address uses plain `http`, not `https`. The password travels across the
network unencrypted. You decided on 2026-09-18 to leave this for now. This
work does not make it worse, and it does not make it better.

---

## Before you start

Work through this list. Every line needs a yes.

1. You can log in to the server. Test it:

   ```
   ssh roe-prod
   ```

   You should get a shell prompt. Type `exit` to come back.

2. You know the site password. Look at it on the server:

   ```
   ssh roe-prod
   sudo grep SITE_PASSWORD /etc/roe_ui.env
   ```

   Write it down somewhere safe. You need it several times.

3. There is at least 15 GB free on the server disk:

   ```
   ssh roe-prod
   df -h /
   ```

   Look at the `Avail` column. If it says less than 15 G, free some space
   first. This command shows what is using the disk:

   ```
   sudo du -h -d 1 /var /home /opt | sort -h | tail -20
   ```

4. You have two hours, and nobody needs the tool during that time.

5. You have told the team that the address will change slightly, and that
   they will be logged out once.

---

## Part 1. Install the two new tools beside the old one

Nothing in Part 1 changes the ROE–OCOD tool. It keeps serving users the whole
time, on port 8000, exactly as now.

If anything in Part 1 goes wrong, you can simply stop. Nothing is broken.

### Part 1A. Three things to do on your laptop first

These three only ever need doing once.

**Step 1. Write down the exact library versions.**

Development uses particular versions of pandas, Splink and the rest. The
server must use the same ones, or results can differ. This command writes
them all to a file.

```
cd ~/PycharmProjects/dedupe_ui/backend
.venv/bin/pip freeze > requirements.lock
```

What you should see: nothing. The command is silent when it works.

Check the file has content:

```
wc -l ~/PycharmProjects/dedupe_ui/backend/requirements.lock
```

You should see a number of about 60 or more, then the filename.

If you see `0`, the virtual environment is empty or missing. Rebuild it
before going on.

Now commit the file:

```
cd ~/PycharmProjects/dedupe_ui
git add backend/requirements.lock deploy docs/DEPLOY.md
git commit -m "deploy: server kit and pinned library versions"
```

**Step 2. Tell git where the server is.**

```
cd ~/PycharmProjects/dedupe_ui
git remote add prod-dedupe roe-prod:/srv/dedupe_ui.git
```

What you should see: nothing. The command is silent when it works.

If you see `error: remote prod-dedupe already exists`, that is fine. It means
you already did this. Carry on.

Check it:

```
cd ~/PycharmProjects/dedupe_ui
git remote -v
```

You should see two lines mentioning `prod-dedupe` and
`roe-prod:/srv/dedupe_ui.git`.

**Step 3. Copy the name-frequency table to the server.**

The donations tool needs a 48 MB reference table. It is too big for git, so
it is copied separately.

```
cd ~/PycharmProjects/dedupe_ui/backend/data/references
scp uk_name_frequencies.parquet uk_name_frequencies.json ubuntu@roe-prod:/home/ubuntu/
```

What you should see: two progress bars, each ending at 100%.

The file is put in place later, in Part 1B step 6, once the folder exists.

### Part 1B. The rest is on the server

Log in now, and stay logged in for the whole of Part 1B:

```
ssh roe-prod
```

**Step 1. Make the user that the new tools will run as.**

This user has no password and no login shell. It exists only to own the data.

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin dedupe_app
```

What you should see: nothing.

If you see `useradd: user 'dedupe_app' already exists`, that is fine. It means
you already did this. Carry on.

Check it:

```
id dedupe_app
```

You should see a line with `uid=`, `gid=` and `groups=`.

**Step 2. Make the folders where the data will live.**

```
sudo mkdir -p /var/lib/dedupe_ui/donations/references
sudo mkdir -p /var/lib/dedupe_ui/donations/tmp
sudo mkdir -p /var/lib/dedupe_ui/psc/references
sudo mkdir -p /var/lib/dedupe_ui/psc/tmp
sudo chown -R dedupe_app:dedupe_app /var/lib/dedupe_ui
sudo chmod 750 /var/lib/dedupe_ui/donations /var/lib/dedupe_ui/psc
```

Check it:

```
ls -la /var/lib/dedupe_ui/
```

You should see `donations` and `psc`, both owned by `dedupe_app`.

**Step 3. Make the folder for the code, and the repository to push into.**

```
sudo mkdir -p /opt/dedupe_ui
sudo chown ubuntu:ubuntu /opt/dedupe_ui
sudo mkdir -p /srv/dedupe_ui.git
sudo chown ubuntu:ubuntu /srv/dedupe_ui.git
git init --bare /srv/dedupe_ui.git
```

What you should see, on the last command:
`Initialised empty Git repository in /srv/dedupe_ui.git/`

If you see `Reinitialized existing Git repository`, that is fine.

**Step 4. Push the code up for the first time.**

Open a second terminal on your laptop. Leave the server one open.

On your laptop:

```
cd ~/PycharmProjects/dedupe_ui
git push prod-dedupe main
```

What you should see: several lines about counting and writing objects, then
a line like `* [new branch] main -> main`.

If you see `Permission denied`, your ssh key is not set up for `roe-prod`.
Fix that first, then run the push again.

Nothing is installed by this push. There is no hook yet. The push only puts
the code where the server can reach it.

**Step 5. Unpack the code on the server.**

Back in the server terminal:

```
git --git-dir=/srv/dedupe_ui.git --work-tree=/opt/dedupe_ui checkout -f main
```

What you should see: nothing.

Check it:

```
ls /opt/dedupe_ui
```

You should see `backend`, `deploy`, `docs` and `frontend`.

**Step 6. Put the name-frequency table in place.**

You copied it to `/home/ubuntu` in Part 1A step 3. Now move it in:

```
sudo install -m 0640 -o dedupe_app -g dedupe_app \
  /home/ubuntu/uk_name_frequencies.parquet \
  /var/lib/dedupe_ui/donations/references/uk_name_frequencies.parquet
sudo install -m 0640 -o dedupe_app -g dedupe_app \
  /home/ubuntu/uk_name_frequencies.json \
  /var/lib/dedupe_ui/donations/references/uk_name_frequencies.json
```

Check it:

```
ls -lh /var/lib/dedupe_ui/donations/references/
```

You should see the `.parquet` file at about 48M, owned by `dedupe_app`.

**Step 7. Make one secret key, to be shared by all three tools.**

This key signs the login cookie. All three tools must use the same one. That
is what makes a single login work across all three.

```
python3 -c "import secrets; print(secrets.token_hex(32))"
```

What you should see: one line of 64 letters and numbers.

Copy that line. You will paste it three times in the next step. Do not
generate it again: it must be the same in all three places.

**Step 8. Write the two settings files.**

First find the site password:

```
sudo grep SITE_PASSWORD /etc/roe_ui.env
```

Now copy the two examples:

```
cp /opt/dedupe_ui/deploy/env/dedupe_donations.env.example /home/ubuntu/dedupe_donations.env
cp /opt/dedupe_ui/deploy/env/dedupe_psc.env.example /home/ubuntu/dedupe_psc.env
chmod 600 /home/ubuntu/dedupe_donations.env /home/ubuntu/dedupe_psc.env
```

Edit the first one:

```
nano /home/ubuntu/dedupe_donations.env
```

Change exactly two lines:

- the line starting `SITE_PASSWORD=` — put the password from above after the
  `=`, with no spaces and no quotes
- the line starting `SECRET_KEY=` — put the 64-character key from step 7
  after the `=`, with no spaces and no quotes

Press `Ctrl+O`, then `Enter`, then `Ctrl+X` to save and close.

Now the second one:

```
nano /home/ubuntu/dedupe_psc.env
```

Change the same two lines, to exactly the same two values.

Save and close the same way.

Now check the keys really do match. This compares them without printing them:

```
diff <(grep -E '^(SITE_PASSWORD|SECRET_KEY)=' /home/ubuntu/dedupe_donations.env) \
     <(grep -E '^(SITE_PASSWORD|SECRET_KEY)=' /home/ubuntu/dedupe_psc.env) \
  && echo "MATCH" || echo "THEY DIFFER - fix them before going on"
```

You should see `MATCH`.

If you see `THEY DIFFER`, open both files again and make the two lines
identical.

Now install them:

```
sudo install -m 0640 -o root -g dedupe_app /home/ubuntu/dedupe_donations.env /etc/dedupe_donations.env
sudo install -m 0640 -o root -g dedupe_app /home/ubuntu/dedupe_psc.env /etc/dedupe_psc.env
```

Check it:

```
sudo ls -l /etc/dedupe_donations.env /etc/dedupe_psc.env
```

Both should show `-rw-r-----` and `root dedupe_app`.

**Step 9. Allow the deploy hook to restart the two new services.**

Read the warning in this step carefully. A mistake in a sudo file can lock
you out of `sudo` on the whole machine. That is why it is checked twice
before it is installed.

```
cp /opt/dedupe_ui/deploy/sudoers/dedupe_deploy /home/ubuntu/dedupe_deploy
sudo visudo -c -f /home/ubuntu/dedupe_deploy
```

What you should see: `/home/ubuntu/dedupe_deploy: parsed OK`

If you see anything else, stop. Do not install the file. The deploy hook will
not work, but nothing is broken, and you can restart the services by hand.

Only if you saw `parsed OK`:

```
sudo install -m 0440 -o root -g root /home/ubuntu/dedupe_deploy /etc/sudoers.d/dedupe_deploy
sudo visudo -c -f /etc/sudoers.d/dedupe_deploy
```

You should see `parsed OK` again.

Now prove it works:

```
sudo -n /bin/systemctl is-active dedupe_donations
```

You should see `inactive` or `unknown`, with no password prompt. That is the
right answer: the service does not exist yet, but sudo allowed the command.

**Step 10. Install the two service files.**

```
sudo install -m 0644 -o root -g root \
  /opt/dedupe_ui/deploy/systemd/dedupe_donations.service \
  /etc/systemd/system/dedupe_donations.service
sudo install -m 0644 -o root -g root \
  /opt/dedupe_ui/deploy/systemd/dedupe_psc.service \
  /etc/systemd/system/dedupe_psc.service
sudo systemctl daemon-reload
```

Check it:

```
systemctl cat dedupe_donations | head -20
```

You should see the file you just installed.

**Step 11. Put the chooser page in place.**

```
sudo mkdir -p /var/www/chooser
sudo install -m 0644 -o root -g root \
  /opt/dedupe_ui/deploy/chooser/index.html \
  /var/www/chooser/index.html
sudo chmod 755 /var/www/chooser
```

Check it:

```
ls -l /var/www/chooser/
```

You should see `index.html`, about 10 kilobytes.

**Step 12. Build the two tools.**

This is the slow step. It takes about five to ten minutes. Splink and pandas
are large.

```
/usr/bin/python3.12 -m venv /opt/dedupe_ui/backend/.venv
/opt/dedupe_ui/backend/.venv/bin/pip install --upgrade pip
/opt/dedupe_ui/backend/.venv/bin/pip install -r /opt/dedupe_ui/backend/requirements.lock
```

What you should see: many lines of `Collecting ...` and `Installing ...`,
ending with `Successfully installed` and a long list.

If you see `/usr/bin/python3.12: No such file or directory`, install it:

```
sudo apt update && sudo apt install -y python3.12 python3.12-venv
```

Then run the three commands above again.

Now build the web pages:

```
cd /opt/dedupe_ui/frontend
npm ci
npm run build
```

What you should see: a summary from `vite` ending in `built in ...`.

Check it:

```
ls /opt/dedupe_ui/backend/static/
```

You should see `assets` and `index.html`.

**Step 13. Install the deploy hook, so future pushes do all of that for you.**

```
sudo install -m 0755 -o ubuntu -g ubuntu \
  /opt/dedupe_ui/deploy/hooks/post-receive \
  /srv/dedupe_ui.git/hooks/post-receive
```

Check it:

```
ls -l /srv/dedupe_ui.git/hooks/post-receive
```

You should see `-rwxr-xr-x` and `ubuntu ubuntu`.

**Step 14. Start the two tools.**

```
sudo systemctl enable --now dedupe_donations
sudo systemctl enable --now dedupe_psc
```

Wait about thirty seconds. The first start builds the database and the
default rules, so it is slower than later starts.

Check them:

```
systemctl is-active dedupe_donations dedupe_psc
```

You should see `active` twice.

If you see `failed`, read the reason:

```
journalctl -u dedupe_donations -n 50 --no-pager
```

The most common causes are a typo in `/etc/dedupe_donations.env`, or the
`dedupe_app` user not being able to write to `/var/lib/dedupe_ui/donations`.

Now check each one answers:

```
curl http://127.0.0.1:8101/donations/api/health
curl http://127.0.0.1:8102/psc/api/health
```

Each should print `{"status":"ok"}`.

Part 1 is done. The ROE–OCOD tool has not been touched. Users have not
noticed anything.

---

## Part 2. Look at the new setup privately, before anyone else sees it

In Part 2 you put the new routing on a private port, 8090. That port is not
open to the internet. Only someone already logged in to the server can reach
it. You reach it through a tunnel from your laptop.

The ROE–OCOD tool is still untouched, still on port 8000, still serving
users.

**Step 1. Put the staging routing in place.**

On the server:

```
ssh roe-prod
```

First compare the file you are about to install with what is running now:

```
diff /etc/caddy/Caddyfile /opt/dedupe_ui/deploy/caddy/Caddyfile.original
```

What you should see: differences in comments and blank lines only.

If you see a difference in a real routing line, one that mentions a web
address or a port, stop. The file in the kit was written from a summary of
the live file, not a copy of it. Edit
`/opt/dedupe_ui/deploy/caddy/Caddyfile.staging` so its Harrier blocks match
the live file exactly, then carry on.

Now check the new file makes sense:

```
sudo caddy validate --adapter caddyfile --config /opt/dedupe_ui/deploy/caddy/Caddyfile.staging
```

You should see `Valid configuration` on the last line.

Save a copy of what is running now, then install the new file:

```
sudo cp -p /etc/caddy/Caddyfile /home/ubuntu/Caddyfile.before-staging
sudo install -m 0644 -o root -g root \
  /opt/dedupe_ui/deploy/caddy/Caddyfile.staging /etc/caddy/Caddyfile
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
```

You should see `Valid configuration` again.

Now reload Caddy. Reload, never restart. A reload does not drop any
connection, so Harrier keeps serving without a blip.

```
sudo systemctl reload caddy
```

What you should see: nothing.

Check Harrier is still fine:

```
systemctl is-active caddy
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8400/
```

You should see `active`, then a number such as `200` or `302`. Any number
other than `000` means Harrier answered.

**Step 2. Run the smoke test on the server.**

Still on the server:

```
SITE_PASSWORD='the-password-you-wrote-down' \
  bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8090
```

Replace `the-password-you-wrote-down` with the real password. Keep the single
quotes around it.

What you should see: a table of checks, then `RESULT: PASSED`.

One line may say `WARN` against `single login`. That is expected at this
point, and it is not a problem. It says the ROE–OCOD tool does not yet share
the secret key. That key is set during the cutover, in Part 3.

If any line says `FAIL`, do not go on to Part 3. The note beside the failing
line says what went wrong.

**Step 3. Look at it in your own browser.**

On your laptop, open a new terminal and run:

```
ssh -L 8090:127.0.0.1:8090 roe-prod
```

Leave that terminal open. It is the tunnel. Closing it closes the tunnel.

Now open this address in your browser:

```
http://127.0.0.1:8090/
```

What you should see: the chooser page, with a red TI mark and three cards.

Click each card in turn and check:

- "ROE–OCOD linkage" opens the tool you already know
- "Donations reconciliation" opens a tool titled `Donations reconciliation`
- "PSC reconciliation" opens a tool titled `PSC reconciliation`

Also type this address in by hand, to check old bookmarks will work:

```
http://127.0.0.1:8090/runs
```

You should get the ROE–OCOD runs list.

Take your time here. This is the version you are about to make live. If
anything looks wrong, fix it now, while nothing is at stake.

When you have finished looking, close the tunnel terminal.

---

## Part 3. The cutover

This is the only part that interrupts users.

### What to expect

The ROE–OCOD tool is unreachable for about thirty seconds to a minute.

Everyone is logged out, once. They log back in with the same password.

After this, a restart will never log anyone out again. That is a side effect
of setting the secret key, and it is an improvement.

### When to do it

Pick a quiet time. Early morning or late afternoon. Not during a deadline.

Tell the team beforehand. Tell them the address stays the same, that they
will see a page with three cards, and that they will need to log in once
more.

### Step 1. Take a backup

Log in to the server:

```
ssh roe-prod
```

Run:

```
bash /opt/dedupe_ui/deploy/scripts/backup_before_cutover.sh
```

This copies all of the ROE–OCOD tool's data, its settings, and the web
routing. It refuses to run if there is less than 10 GB free.

What you should see: six steps, then `Backup finished`, then a folder name
like `/home/ubuntu/backups/20260918-2130`.

Write that folder name down. The rollback needs it.

### Step 2. Do the cutover

Still on the server:

```
SITE_PASSWORD='the-password-you-wrote-down' \
  bash /opt/dedupe_ui/deploy/scripts/cutover.sh
```

Replace `the-password-you-wrote-down` with the real password. Keep the single
quotes around it.

The script will:

1. check that everything is ready, and stop if it is not
2. take another backup
3. run the smoke test on the private port, to prove it all works
4. ask you to type `yes` before it changes anything
5. add the secret key to the ROE–OCOD tool's settings
6. change one line of the ROE–OCOD service file, so it moves to port 8001
7. restart the ROE–OCOD tool — this is the moment users are logged out
8. give port 8000 to Caddy, by reloading it
9. run the smoke test again, on the real address

It stops at the first thing that goes wrong. If it has already changed
something, it offers to roll back, and you answer by typing `yes`.

What you should see at the end: `The cutover is finished`, and a list of the
four addresses.

You can also read the script first. It is written to be read from top to
bottom, and you can run its commands one at a time if you prefer:

```
less /opt/dedupe_ui/deploy/scripts/cutover.sh
```

### Step 3. Check it worked

From your own laptop, open the normal address in a browser. The one ending
in `:8000`.

What you should see: the chooser page with three cards.

Log in. Then check all four of these:

1. Click "ROE–OCOD linkage". You should see the runs list you know.
2. Go back to the chooser page. Click "Donations reconciliation". You should
   see a tool titled `Donations reconciliation`. You should **not** be asked
   to log in again. That is the single login working.
3. Go back again. Click "PSC reconciliation". Again, no second login.
4. Type an old bookmark address by hand, with `/runs` on the end. You should
   get the ROE–OCOD runs list.

If all four work, you are done.

Tell the team. Tell them the address is the same, that there is now a page
with three cards, and that one login covers all three tools.

Keep the backup folder for at least a month.

---

## Part 4. Rollback

### When to use it

Use it if, after the cutover, the ROE–OCOD tool is broken or unreachable, and
you cannot see why within about ten minutes.

Roll back first. Work out the cause afterwards, calmly.

There is no prize for fixing it live.

### What it does

It puts three files back exactly as they were:

- the web routing, `/etc/caddy/Caddyfile`
- how the ROE–OCOD tool is started, `/etc/systemd/system/roe_ui.service`
- the ROE–OCOD tool's settings, `/etc/roe_ui.env`

Then it stops the two new tools.

It deletes nothing. No data is touched, from the old tool or the new ones.

Harrier is not touched. Caddy is reloaded, never restarted, so Harrier does
not drop a single connection.

### The commands

Log in to the server:

```
ssh roe-prod
```

Run this, with the backup folder you wrote down in Part 3 step 1:

```
bash /opt/dedupe_ui/deploy/scripts/rollback.sh /home/ubuntu/backups/20260918-2130
```

Replace `20260918-2130` with your real folder name.

If you cannot find the folder name, leave it off. The script then uses the
most recent backup:

```
bash /opt/dedupe_ui/deploy/scripts/rollback.sh
```

It asks you to type `yes` before it changes anything.

It takes about a minute.

### How to confirm the old tool is back exactly as before

The script checks these itself and prints the answers. You can also check by
hand.

On the server:

```
systemctl is-active roe_ui
```

You should see `active`.

```
sudo grep ExecStart /etc/systemd/system/roe_ui.service
```

You should see `--host 0.0.0.0 --port 8000` on the end of the line. That is
the original.

```
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/runs
```

You should see `200`.

Then run the full check:

```
SITE_PASSWORD='the-password-you-wrote-down' ROE_ONLY=1 \
  bash /opt/dedupe_ui/deploy/scripts/smoke_test.sh http://127.0.0.1:8000
```

You should see `RESULT: PASSED`, with six checks.

Finally, from your own laptop, open the normal address in a browser. The one
ending in `:8000`. You should get the ROE–OCOD tool directly, with no chooser
page, exactly as before.

### One thing to expect after a rollback

Everybody is logged out once more. That is because the settings file goes
back to how it was, and it had no secret key in it.

From then on, each restart of the ROE–OCOD tool logs people out again, as it
always did.

---

## Part 5. Day to day

### Deploying a new version

On your laptop:

```
cd ~/PycharmProjects/dedupe_ui
git add -A
git commit -m "a short note about what changed"
git push prod-dedupe main
```

The server does the rest. You will see its progress printed in your terminal:
which steps it ran, then `Deploy finished`.

It only does the slow steps when it needs to. It installs Python packages
only when `backend/requirements.lock` changed. It installs web packages only
when `frontend/package.json` or `frontend/package-lock.json` changed. It
always rebuilds the web pages, which takes under a minute.

It restarts both new tools. It never touches the ROE–OCOD tool.

**If you changed a Python library**, write the versions down again before you
push, or the server will keep the old ones:

```
cd ~/PycharmProjects/dedupe_ui/backend
.venv/bin/pip freeze > requirements.lock
cd ~/PycharmProjects/dedupe_ui
git add backend/requirements.lock
git commit -m "deploy: update pinned library versions"
git push prod-dedupe main
```

**The ROE–OCOD tool is deployed separately, as it always was:**

```
cd ~/PycharmProjects/roe_ui
git push prod main
```

### When the code ships better default settings

The app seeds a tool's first config version from the profile defaults only when the tool has never been used. After that, a better default shipped with the code changes nothing on the server: every run keeps the version the users have. So after a deploy that changed `backend/app/profiles/defaults/<profile>/linkage_settings.json`, save the new settings as a new config version on each used instance. Any default veto rule the instance does not have is added, switched on. The other rules stay as they are. The version history shows what happened.

```
cd /opt/dedupe_ui/backend
sudo -u dedupe_app PROFILE=donations .venv/bin/python scripts/adopt_linkage_defaults.py \
    --db /var/lib/dedupe_ui/donations/linkage.db --dry-run
```

The dry run lists what would change. Run it again without `--dry-run` to save. It does nothing when the instance has never been used, or when the current version already holds the defaults. Use `PROFILE=psc` and the psc path for the other tool. The next run started from the New run page uses the new version by default. The How it works page and the Thresholds & Splink tab show the lines in force.

### Reading the logs

Each tool writes to the system log. To see the last fifty lines:

```
ssh roe-prod
journalctl -u dedupe_donations -n 50 --no-pager
```

For the PSC tool:

```
ssh roe-prod
journalctl -u dedupe_psc -n 50 --no-pager
```

For the ROE–OCOD tool:

```
ssh roe-prod
journalctl -u roe_ui -n 50 --no-pager
```

For the web routing:

```
ssh roe-prod
journalctl -u caddy -n 50 --no-pager
```

To watch a log as it happens, use `-f` instead of `-n 50`, and press
`Ctrl+C` to stop watching:

```
ssh roe-prod
journalctl -u dedupe_psc -f
```

### Where the data lives

| What | Where |
|---|---|
| Donations tool data | `/var/lib/dedupe_ui/donations` |
| PSC tool data | `/var/lib/dedupe_ui/psc` |
| ROE–OCOD tool data | `/var/lib/roe_ui` |
| All three lots of code | `/opt/dedupe_ui` and `/opt/roe_ui` |

Inside each data folder: `linkage.db` is the database, `uploads` holds files
people uploaded, `runs` holds each run's output, and `references` holds
lookup tables such as the name-frequency table.

Losing the code is nothing. A push puts it back. Losing the data folder loses
the labels people spent hours making.

### Backing up the new tools' data

There is no automatic backup. Run this when you have done a lot of labelling.

```
ssh roe-prod
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /home/ubuntu/backups/dedupe-$TS
sudo tar -C /var/lib -czf /home/ubuntu/backups/dedupe-$TS/dedupe_ui.tar.gz dedupe_ui
sudo chown -R ubuntu:ubuntu /home/ubuntu/backups/dedupe-$TS
ls -lh /home/ubuntu/backups/dedupe-$TS/
```

You should see one `.tar.gz` file and its size.

To bring a copy down to your laptop, run this on your laptop:

```
scp -r ubuntu@roe-prod:/home/ubuntu/backups/dedupe-20260918-2130 ~/backups/
```

Replace `20260918-2130` with the real folder name from the `ls` output above.

### Watching the disk

The server has 126 GB, with about 34 GB free. A PSC run writes a lot of
temporary data. Check the space before you start a big run:

```
ssh roe-prod
df -h /
```

Look at the `Avail` column. If it is under 10 G, do not start a PSC run.

To see what is taking the space:

```
ssh roe-prod
sudo du -h -d 1 /var/lib /home /opt | sort -h | tail -20
```

The temporary files a run makes are cleaned up when the run ends. If a run
was killed, they may be left behind. They are here:

```
ssh roe-prod
sudo du -sh /var/lib/dedupe_ui/psc/tmp /var/lib/dedupe_ui/donations/tmp
```

You can delete files inside those two `tmp` folders safely. Nothing of value
is kept there. Look at what is there before deleting anything:

```
ssh roe-prod
sudo ls -lh /var/lib/dedupe_ui/psc/tmp
```

### What a killed PSC run looks like, and what to do

A PSC run is heavy. The PSC tool is allowed 9 GB of memory. If a run tries to
use more, the system stops that tool, so that the ROE–OCOD tool, Harrier and
Elasticsearch keep working. That is deliberate. It protects everything else.

**What you see in the browser:** the run stops partway and shows as failed.
The progress bar stops moving. Nothing else on the server is affected.

**How to confirm that is what happened:**

```
ssh roe-prod
journalctl -u dedupe_psc -n 80 --no-pager | grep -i -E "killed|oom|memory"
```

If it was a memory kill, you will see a line containing
`A process of this unit has been killed by the OOM killer`, or a line
mentioning `oom-kill`.

**What to do about it, in this order:**

1. Start the run again with fewer rows, to check the rest of the pipeline
   works. There is a quick mode in the run screen for exactly this.

2. Lower the amount DuckDB is allowed before it starts writing to disk.
   Writing to disk is slower but it does not get the tool killed:

   ```
   ssh roe-prod
   sudo nano /etc/dedupe_psc.env
   ```

   Find the line `SPLINK_MEMORY_LIMIT=6GB`. Change it to `4GB`. Save with
   `Ctrl+O`, `Enter`, `Ctrl+X`. Then:

   ```
   sudo systemctl restart dedupe_psc
   ```

3. If a full run still cannot finish, run it on your laptop instead. Your
   laptop has more memory than the tool is allowed on the server. Run it from
   the command line in `~/PycharmProjects/dedupe_ui/backend`, then copy the
   finished run folder up to the server so the team can review it:

   ```
   scp -r ~/PycharmProjects/dedupe_ui/backend/data/runs/<the-run-id> \
     ubuntu@roe-prod:/home/ubuntu/
   ssh roe-prod
   sudo cp -r /home/ubuntu/<the-run-id> /var/lib/dedupe_ui/psc/runs/
   sudo chown -R dedupe_app:dedupe_app /var/lib/dedupe_ui/psc/runs/<the-run-id>
   ```

   Replace `<the-run-id>` with the real folder name in both places. The
   folder to copy is the one under `backend/data/runs/` named after the run.

Do not raise the 9 GB limit in the service file. That limit is what stops a
runaway PSC run from taking the whole server down with it.

---

## Known gaps and risks

These are the things that are not solved. Read them once, now, so none of
them is a surprise later.

### 1. The password travels unencrypted

The address uses plain `http`, not `https`. Anyone who can watch the network
between a user and the server can read the password.

You set this aside on 2026-09-18. It is recorded here so the decision stays
visible. Nothing in this deployment makes it worse.

The fix, when you want it, is to put the tools behind the same `https` that
Harrier already uses on port 8080. Caddy already holds a certificate there.

### 2. Two runs can start at the same time

**This one has a real chance of biting you. Read it properly.**

Each tool allows one run at a time. It does this by remembering, inside
itself, that a run is going. The code is in
`backend/app/services/pipeline_runner.py`. The memory is a variable named
`_active_run_id`, guarded by a lock.

That memory is private to one running process.

The donations tool and the PSC tool are two separate processes. Neither can
see the other's variable. So the guard does not work between them.

**What that means in practice:** if somebody starts a PSC run while a
donations run is already going, both run at once. Together they can ask for
up to 15 GB of memory, on a machine where about 10 GB is free. One of them is
then very likely to be killed.

**What to do until it is fixed:** do not start a run in one tool while a run
is going in the other. Check before you start. Open the runs screen in the
other tool and look at whether anything is in progress.

**The smallest fix, for whoever next works on the backend:** the two
processes need one shared thing they can both see. A lock file in a folder
both can write to is enough. It needs no database and no network.

- Add a shared folder, for example `/var/lib/dedupe_ui/shared`, owned by
  `dedupe_app`, and give both tools the same path through a new setting such
  as `RUN_LOCK_DIR`.
- In `enqueue_run` and `start_run` in
  `backend/app/services/pipeline_runner.py`, take an exclusive lock on a file
  in that folder, using `fcntl.flock` with `LOCK_EX | LOCK_NB`.
- If the lock cannot be taken, do not start. Queue the run, and tell the user
  that the other tool is busy.
- Release the lock in the same place the code already clears
  `_active_run_id`, so a failed run does not leave the lock held.

Both service files would then need `/var/lib/dedupe_ui/shared` added to their
`ReadWritePaths` line.

This has not been written. No backend code was changed by this deployment.

### 3. A full PSC rebuild may not fit on the server

The PSC dataset is large. The PSC tool is allowed 9 GB of memory on the
server, which may not be enough for a full rebuild from scratch.

If it is not, run the rebuild on your laptop and copy the result up. The
steps are in Part 5, under "What a killed PSC run looks like", point 3.

The folder to copy is the run's own folder, under
`~/PycharmProjects/dedupe_ui/backend/data/runs/`, named after the run id. It
goes to `/var/lib/dedupe_ui/psc/runs/` on the server, and must then be owned
by `dedupe_app`.

### 4. The web routing file was written from a summary

The Caddy routing file in this kit,
`/opt/dedupe_ui/deploy/caddy/Caddyfile.original`, was written from a
description of the live file, not from a copy of it. It says the same thing,
but it may not be identical character for character.

This matters in one place only: it is not a safe file to restore from.

So the rollback does not use it. The rollback uses the copy that the backup
script takes straight off the server, before anything changes.

Part 2 step 1 asks you to compare the two files before you install anything.
That comparison is the check that catches any real difference.

### 5. Everything depends on one machine

All three tools, Elasticsearch, Kibana and Harrier are on one server. There
is no second machine. If it is lost, everything is lost with it.

The backups described in Part 5 live on the same machine. Copy them down to
your laptop from time to time. The command is in Part 5, under "Backing up
the new tools' data".
