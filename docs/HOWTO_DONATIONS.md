# How to reconcile donations, from a fresh sheet to an exported file

This is the working guide for the donations tool. It says what to click, what each word on the screen means, and why the tool is built the way it is. Read it in order the first time. After that, jump to the section you need.

The tool takes the Electoral Commission donations sheet. It finds the records that belong to the same donor. It gives each donor one ID that stays the same from run to run. It writes the IDs back into the sheet.

## The words you will meet, in the order you meet them

- **Record.** One row of the sheet you loaded. In this tool a record is one donor entry, with all of that entry's donations underneath it.
- **Earlier grouping.** The manual grouping the team made before this tool existed. The `DonorIDStandardTR` column. The tool reads it as evidence and measures itself against it. It does not treat it as the final answer.
- **Earlier ID.** The ID a record already carried from the earlier grouping.
- **Run.** One pass of the whole method over one sheet, with one saved set of rules.
- **Track.** People and organisations are matched separately. Each is a track.
- **Match key.** A set of columns, such as the company number. Records with the same value in every column of the key are put together without a score.
- **Exact group.** A set of records a match key put together.
- **Guard.** A safety check on a match key. It stops a merge that is not believable, such as one company number under five different names, or one trade union name on more than 200 records.
- **Held group.** A merge a guard stopped. It waits for a person.
- **Unit.** What the scorer compares. One exact group, or one record on its own.
- **Pair.** Two units the scorer compared.
- **Score.** A number from 0 to 1 for one pair. It reads as the chance the two units are the same donor.
- **Bucket.** Where a pair lands: Accepted, For review, or Rejected. The accept line and the review line decide this.
- **Review queue.** The For review bucket. The pairs the score could not settle. This is where you label.
- **Label.** One saved answer about one pair: Match or Not a match. A label beats every score and every rule, in this run and in every later run.
- **Veto rule.** A rule that stops the tool accepting a pair whatever the score says. For example, two people whose birth years are far apart.
- **Cluster.** A set of units joined by accepted pairs. If A matches B and B matches C, the three are one cluster. A cluster is a proposed donor. It is not a decision until you publish.
- **Cluster for review.** A cluster the gate held back for a person. The reasons are listed later in this guide.
- **Part.** When a cluster is really two donors, you sort its units into Part A and Part B. Part A is the first donor and Part B the second. Add Part C for a third.
- **Entity.** One real donor, with one entity ID.
- **Registry.** The durable store of entity IDs. A run is written into it only when it is published.
- **Publish.** Writing a run's proposal into the registry.
- **Retired ID.** An entity ID that lost a merge. It still points to the ID that survived.

## From a fresh sheet to an exported file

### Step 1. Upload the sheet and start a run

1. Open the site and choose Donations. Log in with the shared password.
2. Click **New run** in the left menu.
3. Drop in the Electoral Commission file. It can be the xlsx or a csv. The tool finds the right sheet by itself.
4. Leave the configuration version and the sliders alone the first time. There is one accept line per track, people and organisations, and one review line.
5. Click **Start run**. It takes about half a minute on the full sheet.

Every label already saved is applied to the new run. The earlier grouping is read from the sheet at the same time.

### Step 2. Read the run page

Click the run under **Runs**. The **Summary** tab shows the counts: records, units, pairs in each bucket, entities, and the size of the review queue. The other tabs are:

- **Donors.** The rows as loaded. The cleaned columns sit beside the raw ones, so you can see what each cleaning step did.
- **Exact groups.** The records the match keys put together, and the held groups a guard stopped.
- **Entities.** The proposed answer. One row per entity, with its records and how each record was joined.
- **Publish & export.** Where the answer is saved and downloaded. Come back here at step 5.
- **Diagnostics.** How much each comparison counts, and warnings from scoring.
- **Files** and **History.** The run's files, and what has been done to the run since.

### Step 3. Work through the review queue

Click **Review queue**. Each row is one pair the score could not settle. The two sides are shown next to each other, with the evidence that matters for that kind of donor at the top. Click **Show everything** to see all the columns.

For each pair, click **Match** or **Not a match**. Add a note or a source link if you have one, such as a press report. You do not have to empty the queue. Sort by **Total, highest first** to do the biggest donors first.

The queue can be big. The first run on the full sheet had about 1,200 pairs for review. Labels carry over, so the queue shrinks with every run.

### Step 4. Cluster review

Click **Cluster review**. Each row is one proposed donor the gate held back, or one held group. The reason is printed on the row: Weak link, Mixed earlier IDs, Held by a match key, and so on. The rows are sorted by the total given, biggest first.

Open a row. The top says why it was held. The table lists the units in the group, one per line, with how many records each holds and its earlier ID. Below the table, **How these units are joined** lists every scored pair inside the group. You rarely need that list. It is there so you can check the machine.

Decide one of two things.

- **All the same.** Click it when the whole group is one donor. That is the usual answer for a held trade union.
- **Split into parts.** Set the dropdown on each unit to Part A, Part B, and so on. Click **Suggest parts** to let the tool fill the dropdowns first. Then click **Split into the parts above**.

Either answer is saved as labels. All the same saves a Match between every unit. A split saves a Match inside each part and a Not a match between parts. A unit left unassigned gets no label.

Your decisions are saved as soon as you make them. The groups and the entity IDs are rebuilt when you click **Apply decisions and recluster** at the top of the page.

### Step 5. Publish

Open the run, then **Publish & export**. Click **Publish this run**. This writes the entity IDs into the registry. An entity ID stays the same in every later run. An ID that lost a merge becomes a retired ID and still points to the survivor. Nothing is stored until you publish, so a run you are not happy with can simply be left.

### Step 6. Export

On the same tab, download **Excel** or **CSV**. Under **This run's proposal** you get what the run proposes. Under **The published registry** you get what has been published. The Excel file is the sheet as you uploaded it, with the entity ID added at the end, plus a sheet of retired IDs and a sheet naming the run and the rules version. **Retired IDs** on its own is the full list of old IDs and where they now point.

### Step 7. Next time

Upload the new sheet and start a new run. Every saved label is applied again. Only pairs that are new or changed come back to the queue. Publish when you are happy.

## A worked example: the House of Commons group

The biggest row in the first run's cluster review was C-103584. It is 26 units and 365 records, worth about 207 million pounds, all public funds. The names include House of Commons, House of Lords, Department of Finance & Administration, Fees Office in many spellings, Department of Resources, and Corporate Services, Tŷ'r Cyffredin, which is Welsh for House of Commons.

The gate held it for two reasons. One pair inside scored only 0.165, so the group may be a chain. And the records carry two earlier IDs, 32723 and 32727.

What happened is this. "House of Commons Fees Office" and "House of Lords Fees Office" scored 0.997 against each other on the name alone. That one strong pair joined the Commons units to the Lords units. Every other pair inside is fine. Only the whole is wrong. That is what a chain looks like, and it is why the tool checks groups and not just pairs.

The earlier grouping had it right: 32723 is the House of Commons side and 32727 the House of Lords side. To reconcile it:

1. Click **Suggest parts**. Because the group has two earlier IDs, the tool puts each unit into a part by its earlier ID.
2. Check the table. Everything Commons should be in one part: House of Commons, the Fees Office spellings, Department of Finance & Administration, Department of Resources, and the Welsh names. Everything Lords should be in the other.
3. Fix any dropdown that is wrong.
4. Click **Split into the parts above**, then **Apply decisions and recluster**.

After that the group is two entities. The labels are saved, so the next run keeps them apart without asking.

One limit to know about. Three units in this group carry both earlier IDs. "Department of Finance & Administration", with 70 records, is one of them. That means the earlier grouping split those records by something other than the name, probably by which House the money went to. On this screen a unit moves as a whole. If those records need splitting record by record, that is a question for the rules, not for review. It is the kind of thing to raise with whoever made the earlier grouping.

## Why the tool works in groups

People ask why there is a cluster review at all. Why not just look at each donor and fix it? The answer is that this is exactly what cluster review is. A cluster is a proposed donor that has not been published yet. The page is the donor reconciliation page. The word "cluster" and the list of scored pairs make it look like something else.

Here is how the design got there.

1. The scorer compares two units at a time. A label is an answer about one pair. That was kept from the earlier tool on purpose, because the reviewer works best with the evidence for two things side by side.
2. The output has to be one ID per record. To get from pairs to IDs, the accepted pairs must be joined up. If A matches B and B matches C, the three become one group. Every method that scores pairs has this step. The scoring engine itself does it.
3. Joining up has one known failure: chains. Each pair passes, but the whole is wrong. The House of Commons group is one. A pair screen cannot see this, because every pair looks fine on its own. So the tool needed a check on the whole group. That is the gate, and cluster review is where a person answers it.

The other ways of doing it were worse.

- No group check at all. Chains would be published silently. The House of Commons and the House of Lords would have gone out as one donor.
- Pair review only. You would have to find the one wrong pair among fifty. The gate finds the group for you and shows you the weak pair.
- Editing entities after publishing. Harder to undo, and the tool learns nothing from it. Saving a group decision as labels means the next run remembers, and the model can learn from it.

## What the rows in the cluster review queue mean

- **Weak link.** Two units inside the cluster scored very low against each other. The cluster may be a chain. Look for the join that should not be there.
- **Mixed earlier IDs.** The cluster joins records the earlier grouping gave different IDs. Either the earlier grouping was too fine, or the tool has over-merged. Suggest parts splits it by earlier ID so you can see which.
- **Held by a match key.** A guard stopped a match key merging these records. They are still separate. The usual answer is All the same.
- **Earlier ID spans tracks.** One earlier ID covers a person and an organisation. The tool keeps them as two entities and shows you both.
- **Conflict.** A reviewer has already said two of these records are not the same, and the run joined them anyway.
- **Too large.** More units than the limit allows. Usually a match key is too loose or the accept line is too low.
- **Mixed names.** More different names, or birth years, than one person could have.
- **Value undecided.** Two values for a column were equally common, so one value for the whole entity could not be settled.

## The model, after about fifty labels

On the run page there is a model panel for each track. Click **Train from this run**. The model learns from your labels which evidence matters. At first it is a new model. It only reorders the review queue. Once it has been graded against labels held back for the purpose, it can set the buckets itself. After training, sort the queue by **Most useful to label**. That puts first the pairs the model is least sure about.

## Where to look when something seems wrong

- **Label library.** Every label ever saved, who saved it and when. You can withdraw one. The row stays, so nothing is lost.
- **Audit log.** Every action the tool took.
- **How it works.** The method, in the same words as this guide.
- **Config & rules.** The cleaning steps, match keys, guards, veto rules and thresholds. Each veto rule has an On switch, so a rule can be turned off for a run without deleting it. Every change makes a new version, and each run names the version it used. Leave this alone until the process feels familiar.

## Two questions still open

- The trade union guard holds any name on more than 200 records. Eight unions are held on the first run. Raising the limit to 1,200 clears all eight with no new conflict. Keep 200, or raise it?
- For individuals, the evidence shown first is the recipient, the local unit, the amount, the date and the type. Should the reporting period, or whether the donation was a bequest, be shown first too?
