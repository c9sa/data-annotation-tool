# Data Annotation Tool

Local tool to label text from CSV & JSON files.

## Requirements

- Python 3.10 or later

## Start

Open a terminal in the tool folder. Run:

```powershell
python app.py
```


## Load a file

Drop a CSV or JSON file into the page, or select a file.

- CSV files must have a header row.
- JSON files must contain an array of objects.

Select the text column. Configure up to nine labels. Select single-label or multiple-label mode.

## Labelling Modes

### Fast pass

Uses number keys & automatically iterates to the next asample

### Review

For multi-labels. Disables auto iteration.

Add secondary labels if necessary.

Press Enter to save and continue.


### Keyboard shortcuts

| Key | Action |
|---|---|
| 1–9 | Select a primary label |
| Shift+1–9 | Select or remove a secondary label in Review |
| Enter | Save and continue in Review |
| Left / Right | Open the previous or next message |
| S | Skip the message |
| R | Flag the message for review |
| U | Undo the last change |


## Save and export

The tool saves a working copy in `working/`. It does not change the source file.

Use the recent-session list to continue a saved session.

Open More, then Export:

- **Labeled file:** Export all rows with their labels and annotation status.
- **Training CSV:** Export completed rows as `text,label`. The label is the primary label.

## Note

If a file has secondary labels, you cannot change it to single-label mode.
