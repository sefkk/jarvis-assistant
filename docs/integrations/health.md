# Apple Health workouts

Jarvis keeps a training log and answers questions such as "How often did I run this week?" or "Compare my
cycling this month with last month". Workouts you log by talking to Jarvis go there directly; workouts
recorded by your iPhone or Apple Watch come from Apple Health, which needs a small bridge because there is no
Health app on the Mac.

The bridge is a folder in iCloud Drive (the "inbox"): the iPhone writes workout files into it, and Jarvis
imports them on the Mac. Health data stays on your devices and in iCloud Drive; it is validated like any file
but does not count as untrusted content, and the logs record only counts and ids.

## What you need

- An iPhone with the Shortcuts app and iCloud Drive turned on, on the same Apple Account as the Mac.
- About 20 minutes to build the Shortcut once.

Three routes exist; the first is the one to set up, the others are fallbacks:

| Route | Needs | When |
|---|---|---|
| 1. iPhone Shortcut > iCloud Drive inbox | Shortcuts app | Main route, free, runs by itself |
| 2. A Health export app > the same inbox | A third-party app (for example Health Auto Export) | If the Shortcut cannot read enough details |
| 3. Full Health export (`export.zip`) | Health app > Export All Health Data | One-off import of your whole history |

## Set it up

### 1. Create the inbox folder

On the iPhone, in the Files app: **iCloud Drive** > New Folder `Jarvis` > inside it New Folder `health_inbox`.
On the Mac it appears at `~/Library/Mobile Documents/com~apple~CloudDocs/Jarvis/health_inbox`, which is the
default of `[training] health_inbox` in `config.toml`. Another folder works too: set `health_inbox` to it
(`~` is expanded).

If the Shortcut's *Save File* action refuses to write outside `iCloud Drive/Shortcuts/`, create
`Shortcuts/Jarvis/health_inbox` instead and set:

```toml
[training]
health_inbox = "~/Library/Mobile Documents/com~apple~CloudDocs/Shortcuts/Jarvis/health_inbox"
```

### 2. Look at what your iOS version exposes

Apple does not document which details a workout exposes in Shortcuts, and it changes between iOS versions.
Check before you build:

1. Shortcuts app > **+** > add **Find Health Samples**, set **Type** to **Workouts** (if there is no such entry,
   look for an action called **Find Workouts**).
2. Add **Get Details of Health Sample** with the result and open its **Detail** menu.
3. Note what is offered. Typical details: Start Date, End Date, Duration, Workout Type (or Name / Type), Total
   Distance, Total Energy Burned (or Active Energy), Average Heart Rate, Source.

Only the **workout type** and the **start date** are required. Everything else is optional.

### 3. Build the Shortcut "Jarvis Health Export"

1. **Find Health Samples**: Type **Workouts**, filter **Start Date is in the last 2 days**, sort by Start Date,
   oldest first. (Two days overlap on purpose; Jarvis skips workouts it already has.)
2. **Repeat with Each** item. Inside the loop:
   1. **Get Details of Health Sample** > **Start Date**, then **Format Date** with **ISO 8601** (time included).
      The same for **End Date**. Result: `2026-10-10T18:30:00+02:00`.
   2. **Get Details of Health Sample** for the other details you found: Duration, Workout Type, Total Distance,
      Total Energy Burned, Average Heart Rate, Source. If a unit can be picked, choose **km** (or **m**) for
      distance and **kcal** for energy.
   3. **Dictionary**: one workout with the keys from the table below, filled with the variables from the steps
      above. Leave out what you do not have.
   4. **Add to Variable** `Workouts`.
3. After the loop: **Dictionary** with `format` (Text) = `jarvis-shortcut` and `workouts` (Array) = the
   variable `Workouts`.
4. **Text** containing only that Dictionary (this turns it into JSON text). If your iOS version does not render
   it as JSON, skip this step and give the Dictionary straight to *Save File* with a `.json` name.
5. **Format Date** of **Current Date** with the custom format `yyyy-MM-dd-HHmm`, saved as variable `Stamp`.
6. **Save File**: service iCloud Drive, **Ask Where to Save** off, path `Jarvis/health_inbox/workouts_` +
   `Stamp` + `.json`, **Overwrite If File Exists** on.
7. Run it once by hand, then on the Mac:

   ```
   jarvis health sync
   ```

   It prints how many files and workouts were imported.

### 4. Run it automatically

Shortcuts app > **Automation** > **+** > **Time of Day**, for example 22:00 daily, **Run Immediately** (turn
"Ask Before Running" off), choose "Jarvis Health Export". Or use the **Workout** trigger "when any workout
ends"; both together are fine, duplicates are skipped.

On the Mac, `[training] sync_on_startup = true` (the default) imports new files whenever Jarvis starts;
`jarvis health sync` imports them right away.

### The file format

```json
{
  "format": "jarvis-shortcut",
  "version": 1,
  "workouts": [
    {
      "id": "A1B2C3D4-0001-4000-8000-000000000001",
      "type": "Running",
      "start": "2026-10-10T18:30:00+02:00",
      "end": "2026-10-10T19:00:00+02:00",
      "duration": "0:30:12",
      "distance": {"qty": 5.2, "units": "km"},
      "energy": {"qty": 410, "units": "kcal"},
      "avgHeartRate": 152,
      "maxHeartRate": 178,
      "source": "Apple Watch"
    }
  ]
}
```

| Key (alternatives) | Required | Accepted forms |
|---|---|---|
| `type` (`workoutType`, `name`) | yes | Text: "Running", "Outdoor Run", "Traditional Strength Training" ... normalized to running, walking, cycling, strength, hiit ...; unknown words are kept. |
| `start` (`startDate`) | yes | ISO 8601 with offset, `2026-10-10 18:30:00 +0200`, or epoch seconds. |
| `end` (`endDate`) | no | Same formats; computed from `duration` when missing. |
| `duration` | no | Seconds, `"H:MM:SS"`, `"MM:SS"`, or text with a unit (`"45 min"`, `"1.5 h"`). Add `durationUnit: "min"` for a plain number in minutes. |
| `distance` (`totalDistance`) | no | Metres, text with a unit (`"5.2 km"`) or `{"qty": 5.2, "units": "km"}`. Units: m, km, mi, yd. |
| `energy` (`activeEnergy`, `totalEnergy`, `calories`) | no | kcal, text with a unit, or `{"qty": 410, "units": "kcal"}`. Units: kcal/Cal, kJ. |
| `avgHeartRate` (`heartRate`) | no | Number (bpm), `{"qty": ..}`, or `heartRate: {"avg": .., "max": ..}`. |
| `maxHeartRate` | no | Number or `{"qty": ..}`. |
| `id` (`uuid`) | no | Any text up to 128 characters; without it, type + start + end identify the workout. |
| `source`, `notes` | no | Text (notes up to 500 characters). |

A plain JSON list of workouts, or `{"workouts": [...]}` without `format`, works too. Extra keys are ignored.

### Fallback: a Health export app

Apps such as **Health Auto Export** can write workouts as JSON into the same inbox on a schedule. Choose
**Workouts** only, format **JSON** (Jarvis reads both its version 1 and version 2 layouts) and the folder
`Jarvis/health_inbox`. Do not run the app and the Shortcut for the same days: a workout with an id from one and
none from the other would be stored twice.

### Full history: the Health export

1. iPhone: Health app > your picture (top right) > **Export All Health Data** > Export. This takes a while.
2. Send `export.zip` to the Mac (AirDrop, or save it to iCloud Drive).
3. On the Mac:

   ```
   jarvis health import ~/Downloads/export.zip
   ```

   The archive can be several GB; Jarvis streams it and reads only the workouts. Running it again is safe. An
   already extracted `export.xml` works too.

## macOS permissions

None on the Mac. On the iPhone, the Shortcut asks once for Health access; allow reading workouts.

## Check that it works

- `jarvis doctor`: `Health inbox` (the folder), `Health inbox pending` (files waiting) and `Health sync`
  (workouts imported, last import).
- In the chat: "List my workouts this month" or "How often did I run this week?"

## Common problems

| Problem | Fix |
|---|---|
| `Health inbox  folder missing` | Create the folder (step 1) or point `health_inbox` to the right one. If you do not use Health at all, ignore this line. |
| `Health inbox pending  N file(s) waiting` | Run `jarvis health sync`, or restart Jarvis. |
| A file lands in `health_inbox/failed/` | Read the `.error.txt` next to it: usually a missing `type` or `start`, or text that is not JSON (see step 4 of the Shortcut). |
| The Shortcut cannot save the file | Use the `Shortcuts/Jarvis/health_inbox` path (step 1). |
| Workouts appear twice | Two routes exported the same days. Use one route. |
| Nothing arrives on the Mac | iCloud Drive must be on for both devices; open the folder in Finder to make it download. |
