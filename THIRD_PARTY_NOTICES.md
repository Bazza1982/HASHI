# Third-Party Notices

## Kokoro 0.9.4 and Kokoro-82M

HASHI installs Kokoro only in its isolated optional TTS Function runtime. The
standard deployment prepares the selected Kokoro-82M model and voice files on
the user's machine; HASHI also distributes short synthetic OGG/MP3 previews
generated with the configured voices.

- Python package: `kokoro==0.9.4`
- Model and voice repository: <https://huggingface.co/hexgrad/Kokoro-82M>
- Prepared model revision: `f3ff3571791e39611d31c381e3a41a3af07b4987`
- Upstream voice catalogue: <https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md>
- Upstream license metadata: Apache License 2.0

The configured Japanese male voice `jm_kumo` (upstream SHA-256 prefix
`98340afd`) identifies Koniwa's `tnc__kumonoito` material as its CC BY source.
The TNC audio is licensed under CC BY 3.0; its source text is Public Domain.
Attribution: Television Nishinippon announcer readings (March 2020), catalogued
by the Koniwa project. HASHI's files are newly synthesized excerpts from the
model and converted to OGG/MP3; they are not copies of the source recordings.

- Koniwa project and attribution table: <https://github.com/koniwa/koniwa>
- Source record: <https://github.com/koniwa/koniwa/blob/master/source/tnc/tnc__kumonoito.txt>
- CC BY 3.0: <https://creativecommons.org/licenses/by/3.0/>

## usecomputer 0.1.11

HASHI includes the upstream Linux x86-64 `usecomputer` executable at
`tools/bin/usecomputer` for its optional Linux desktop-control fallback.

- Upstream: <https://github.com/remorses/usecomputer>
- Package: `usecomputer@0.1.11`
- Source artifact: `dist/linux-x64/usecomputer` from the published npm tarball
- SHA-256: `6ed5444144e08de1225a64f3e990115748fb854eece7aaffc0469c5898d438e4`
- License: MIT

```text
MIT License

Copyright (c) 2026 Tommy D. Rossi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
