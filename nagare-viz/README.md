# Nagare workflow editor

Run the editor with `npm ci` followed by `npm run dev`.

Validation:

```text
npm test
npx playwright install chromium
npm run test:ui
npm run build
```

The UI check uses fixture data and a temporary server to verify visible save errors, retained edits, and document scoping. It checks that a late save response cannot clear edits in a different workflow. The browser and server close on completion.

A failed or unconfirmed save retains the draft. The editor shows the available server reason beside the toolbar. A successful save requires a positive JSON acknowledgment; an unavailable response requires checking the file before retrying.
