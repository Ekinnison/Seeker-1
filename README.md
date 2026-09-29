# Auction Scanner

Every 30 minutes, this checks auction sites for equipment that matches your rules (item type, price, distance, condition). When it finds a match, it sends a push notification to your phone with a button that opens the listing. It runs for free on GitHub, so no computer needs to stay on.

## One-time setup (about 20 minutes)

**1. Phone notifications (free).** Install the **ntfy** app (iOS or Android). Tap **+** and subscribe to a topic name you make up, such as `evan-auctions-k8x2q7`. Anyone who knows the name can see your alerts, so make it hard to guess and treat it like a password.

**2. GitHub repository.** Create a free GitHub account, make a new repository, and upload every file in this folder, including the hidden `.github` folder.
- A **public** repo gets unlimited free run time. Nothing sensitive is stored in it: your topic name stays in a secret, and your home location is only a city.
- A **private** repo gets 2,000 free minutes a month. That covers roughly hourly runs, so change the cron line in `.github/workflows/scan.yml` to `"7 * * * *"`.

**3. Add your topic as a secret.** In the repo, go to Settings → Secrets and variables → Actions → New repository secret. Name it `NTFY_TOPIC` and set the value to your topic name.

**4. Test the connection.** Go to Actions → Auction scan → Run workflow, choose mode `test-notify`, and run it. You should get a notification within a minute.

## Adding an auction site

Each site lays out its pages differently, so each one needs a short setup:

1. Search the site in your browser (for example, "mower"). Copy the URL and replace your search word with `{query}`.
2. Go to Actions → Run workflow, choose mode `inspect`, and paste the URL.
3. Open the finished run's log and copy the output under "Run scanner". Paste it to Claude and ask for the `selectors` block.
4. Add the site under `sources:` in `config.yaml` with `enabled: true`.
5. Run mode `test`. The log lists every listing found, whether it matched, and why.

Sites worth trying: HiBid (many local auctioneers), Purple Wave, BigIron, AuctionTime, Proxibid, EquipmentFacts, and local auctioneers' own websites. Check each site's terms of use, and keep the scan frequency modest. If a site requires login or blocks automated browsers, skip it and use its built-in saved-search email alerts instead.

## Day-to-day

- **Change what you're hunting for:** edit `watches:` in `config.yaml`. Each watch is commented.
- **Pause a watch or site:** set `enabled: false`.
- **Duplicates:** you're alerted about each listing only once. The history is kept in `state/`.
- **Broken sites:** if a site returns nothing for about 6 hours straight, you'll get a "may be broken" alert. That usually means the site changed its layout, so run `inspect` again.
- **Inactivity:** GitHub pauses scheduled runs in public repos after 60 days with no activity. It emails you first, and re-enabling takes one click.
