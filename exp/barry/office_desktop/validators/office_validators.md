# Office EXP Validators

Use these checks before treating an Office EXP run as successful.

## Word

- File exists and opens in Word.
- `.docx` contains expected headings.
- Tables are real Word tables when tables are part of the task.
- Footer or page number exists when requested.
- PDF output starts with `%PDF` when a PDF is requested.
- Screenshot evidence shows the final document in Word UI.

## Excel

- File exists and opens in Excel.
- Representative formulas match the expected formulas.
- Formula results are populated.
- AutoFilter range exists when filtering is part of the task.
- Conditional formatting count is greater than zero when requested.
- Chart count is greater than zero when charting is requested.
- Screenshot evidence shows the workbook in Excel UI.

## PowerPoint

- File exists and opens in PowerPoint.
- Slide count matches the requested deck plan.
- Final slide order matches the intended order.
- Notes slides exist and notes are non-empty when presenting is part of the task.
- Visual elements do not overlap in key screenshots.
- Slideshow can start, advance, and exit.

## Cross-application

- Native Office files are saved before derived exports.
- Clipboard or embedded visuals are validated in the destination application.
- Final report records evidence paths and any recovery steps used.
