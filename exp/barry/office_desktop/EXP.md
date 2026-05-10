# Barry Office Desktop EXP

This EXP captures Office desktop expertise learned in Barry's HASHI Windows
environment. It is tailored, evidence-driven, and context-specific.

## Definition

EXP is context-specific expertise and experience learned through repeated
execution, evidence, failures, templates, and user preference.

In this domain, EXP means the agent knows how to produce higher-quality Word,
Excel, and PowerPoint results for Barry by combining:

- `use_computer` for real desktop execution and validation
- `windows_helper` for stable input, screenshots, drag, focus, and clipboard paths
- Office object-level automation when layout quality or document structure needs
  precise control
- screenshots, generated files, and validators as evidence

## Context

- User: Barry
- Machine: HASHI Windows desktop
- Applications: Microsoft Word, Excel, PowerPoint
- Known preference: polished, practical business output with clean structure,
  restrained text density, reliable charts, and evidence-backed validation

## Operating principles

- Use UI actions when the goal is to validate real desktop capability.
- Use helper or object-level automation when quality, speed, or layout precision
  matters more than proving low-level mouse and keyboard execution.
- Always leave evidence: output files, screenshots, and validation notes.
- Treat failures as memory. Record the symptom, cause, recovery, and evidence.
- Do not assume this EXP transfers to another user or machine without
  revalidation.
- Train improvements with real templates, examples, clear goals, repeated
  practice, validators, and failure memory before treating them as stable.

## Known high-value lessons

- Word PDF export should use File > Export > Create PDF/XPS. Renaming a Save As
  target to `.pdf` can produce `.pdf.docx`.
- On HASHI1 after migration, Word automation must use a docx-first route.
  `Documents.Add() -> SaveAs2()` hangs in this environment, while opening an
  existing `.docx` and exporting PDF works. Use
  `python3 tools/word_docx_first.py smoke|export` for local Word evidence.
- Excel AutoFilter is more stable through the legacy key path `Alt+D,F,F` than
  relying on `Ctrl+Shift+L` in this environment.
- PowerPoint quality is poor when large text blobs are pasted into placeholders.
  Prefer fixed layouts, object-level positioning, short text, and shape-based
  charts.
- For Office data entry, clipboard paste is often more reliable than simulated
  keystrokes for tables and multi-line content.

## Available playbooks

- `word`: advanced report formatting and export behavior
- `excel`: data analysis, formulas, filters, chart validation
- `powerpoint`: polished deck generation, notes, rearranging, presentation test
- `integrated_workflows`: Excel-to-Word-to-PDF and cross-app evidence paths
