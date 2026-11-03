# Multiline frontend text

The common single-line identity validator stays unchanged. Bounded human text
accepts LF, CRLF and tabs while rejecting non-text C0 controls and DEL. Its existing
length limits remain: control text 65,536, generic command prose/value/table cells
4,096 and hints 1,024. Key labels, table headers, routing IDs and action identities
remain single-line.

Command text positions follow existing command owners: steer/focus directions,
ticket descriptions, hchat/wa_send message arguments after the target, and loop
task descriptions. Loop list/stop selectors remain strict, including the owner's
existing stop-prefix behavior. Other command arguments remain strict. The existing
sys prompt contract and its separate size limit are retained. Neither text
validation nor supplied authorization fields grant execution rights.

Verified reachable paths include the compatibility parser, durable Telegram
command admission and replay, Session cancellation reasons, and durable
presentation/feed reload. This does not implement a new control/steer endpoint.
Hints and key/value bodies keep paragraphs. HTML table rendering also retains
escaped headers/rows rather than omitting the table; no cell text becomes HTML.

Fixtures retain original argument/label limits, reject routing/control-character
injection and traverse actual admission, Session and projection/render consumers.
No model request, external message, scheduler task or live cancellation is issued.
