# Plan: Fix Project / Phase / Task Layer Architecture

**Date:** 2026-04-06
**Status:** Approved for implementation
**Scope:** AIPM persistence layer, Minato backend adapter, Nagare run metadata, frontend Shimanto panel

---

## 1. Problem Statement

The current codebase collapses two distinct architectural layers into one table:

| Intended Layer | Current Reality |
|---|---|
| Minato Project (top-level workspace) | Stored in `projects` table |
| Shimanto Phase (belongs to a Project) | **Also stored in `projects` table** — no distinction |
| Task (belongs to a Phase) | `tasks.project_id` → projects, `tasks.milestone_id` → milestones |
| Nagare Workflow (library YAML) | Referenced by `tasks.nagare_workflow_path` — correct |
| Nagare Run | No linkage back to the Task that triggered it |
| Artefacts / KASUMI objects | Belong to Phase via filesystem folder, no phase_id FK |

**Phase (`shimanto` layer) has never been implemented as a database entity.** The `milestones` table was originally designed as a child of Project, serving a legacy date-driven Gantt concept from an adopted external codebase — not the Shimanto phase concept.

---

## 2. Correct Target Architecture

### 2.1 Conceptual Hierarchy

```
Minato Project           (top-level workspace, no mandatory dates)
    └── Shimanto Phase   (execution unit under a project, has date OR steps mode)
            ├── Milestone (optional, date-driven mode only — key checkpoint within phase)
            ├── Task      (atomic unit of work, belongs to phase)
            │       └── nagare_workflow_path  (optional — links to Nagare library YAML)
            │           └── Nagare Run        (execution instance, records task_id)
            └── WAREHOUSE folder              (real filesystem path, scoped to phase)
```

### 2.2 Relationship Rules

| Relationship | Type | Nullable |
|---|---|---|
| Phase → Project | many-to-one (FK) | NOT NULL |
| Task → Phase | many-to-one (FK) | NOT NULL |
| Task → Milestone | many-to-one (FK) | NULLABLE (only in date-driven mode) |
| Milestone → Phase | many-to-one (FK) | NOT NULL (change from project_id) |
| Nagare Run → Task | many-to-one (stored in run metadata) | NULLABLE (not all runs are from a task) |
| Phase → WAREHOUSE | one folder per phase (real path stored in `folder_path`) | NULLABLE |

---

## 3. Database Changes (AIPM)

### 3.1 New Table: `phases`

```python
class Phase(Base):
    __tablename__ = "phases"
    id            = Column(String, primary_key=True, default=generate_uuid)
    project_id    = Column(String, ForeignKey("projects.id"), nullable=False)
    code          = Column(String, nullable=False)
    name          = Column(String, nullable=False)
    description   = Column(String, nullable=True)
    status        = Column(SQLEnum(PhaseStatus), default=PhaseStatus.Draft)
    owner         = Column(String, nullable=True)
    sequence_order = Column(Integer, default=0)
    timeline_mode  = Column(String, default="steps", nullable=False)  # "steps" | "calendar"
    planned_start  = Column(Date, nullable=True)   # only used in calendar mode
    planned_end    = Column(Date, nullable=True)
    actual_start   = Column(Date, nullable=True)
    actual_end     = Column(Date, nullable=True)
    # Warehouse / filesystem binding (real path, not ID-based)
    folder_path    = Column(String, nullable=True)
    created_at     = Column(DateTime, default=utcnow)
    updated_at     = Column(DateTime, default=utcnow, onupdate=utcnow)
    version        = Column(Integer, default=1)
    is_trashed     = Column(Boolean, default=False, nullable=True)
    trashed_at     = Column(String, nullable=True)

    project    = relationship("Project", back_populates="phases")
    milestones = relationship("Milestone", back_populates="phase")
    tasks      = relationship("Task", back_populates="phase")
```

New enum needed:
```python
class PhaseStatus(str, enum.Enum):
    Draft       = "Draft"
    Planned     = "Planned"
    In_Progress = "In_Progress"
    Completed   = "Completed"
    Cancelled   = "Cancelled"
    On_Hold     = "On_Hold"
```

### 3.2 Changes to `projects` table

Remove: `planned_start`, `planned_end`, `actual_start`, `actual_end` — dates now live at Phase level.
Add relationship: `phases = relationship("Phase", back_populates="project")`
Remove relationship: `milestones` (milestones now under phases, not projects)

### 3.3 Changes to `milestones` table

Change: `project_id` → `phase_id` (FK to `phases.id`, NOT NULL)
Remove: `project` relationship
Add: `phase` relationship

```python
# Before
project_id = Column(String, ForeignKey("projects.id"))
project = relationship("Project", back_populates="milestones")

# After
phase_id = Column(String, ForeignKey("phases.id"), nullable=False)
phase = relationship("Phase", back_populates="milestones")
```

### 3.4 Changes to `tasks` table

Add: `phase_id = Column(String, ForeignKey("phases.id"), nullable=False)`
Keep: `project_id` as a denormalized convenience field (nullable, for fast queries)
Keep: `milestone_id` as nullable (only populated in calendar/date-driven mode)
Add: `nagare_run_id = Column(String, nullable=True)` — records the Nagare run triggered by this task
Add: `phase` relationship

```python
phase_id      = Column(String, ForeignKey("phases.id"), nullable=False)
nagare_run_id = Column(String, nullable=True)   # FK is soft — Nagare DB is separate
phase         = relationship("Phase", back_populates="tasks")
```

### 3.5 Other tables that reference `project_id`

These tables also have `project_id` and may need `phase_id` added if they are scoped to a phase:

| Table | Action |
|---|---|
| `raids` | Add optional `phase_id` — RAIDs can be scoped to a phase |
| `weekly_updates` | Keep at project level only |
| `resource_allocations` | Add optional `phase_id` |
| `export_batches` | Keep at project level only |
| `change_sets` / `change_items` | No change needed |

---

## 4. Data Migration

### 4.1 Identify current records that are Phases (not Projects)

From current `projects` table, the following are Shimanto Phases, not Minato Projects:

| code | name | True role | parent Project |
|---|---|---|---|
| READ | Reading | Phase | Barry's PhD |
| SHIMANTO | Shimanto Project Management System | Phase | Barry's PhD |
| TESTING1 | Testing 1 | Phase (test data) | Barry's PhD |

True Projects:
| code | name |
|---|---|
| BARRY_S_PHD | Barry's PhD |
| BARRY_PHD | Barry' PhD (duplicate — merge or delete) |

### 4.2 Migration steps (run as a one-time script)

```
1. Create phases table (no breaking schema change yet)
2. Insert Phase records for READ, SHIMANTO (parent_project_id = BARRY_S_PHD)
3. Update tasks.phase_id for any existing tasks that belong to those phases
4. Update milestones.phase_id for any existing milestones
5. Remove Phase-like records from projects table (soft-delete: is_trashed=True)
6. Remove duplicate BARRY_PHD project (keep BARRY_S_PHD)
```

Migration script location: `/home/lily/projects/AIPM/migrations/001_add_phases_table.py`

---

## 5. API Changes (AIPM / Minato backend)

### 5.1 New Phase endpoints

```
GET    /api/phases                          → list all phases (filterable by project_id)
GET    /api/phases/{phase_id}               → get single phase
POST   /api/phases                          → create phase (body: project_id, name, ...)
PATCH  /api/phases/{phase_id}               → update phase
DELETE /api/phases/{phase_id}               → soft-delete phase
GET    /api/projects/{project_id}/phases    → list phases under a project
```

### 5.2 Updated Task endpoints

```
POST   /api/tasks                           → now requires phase_id
GET    /api/phases/{phase_id}/tasks         → list tasks under a phase
```

### 5.3 Updated Shimanto/Projects endpoint

```
GET    /api/shimanto/projects               → returns ONLY true Minato Projects
                                              (was incorrectly returning all rows from projects table)
GET    /api/shimanto/phases                 → new — returns phases for a given project_id
```

---

## 6. Nagare Run Linkage

### 6.1 Problem

Currently Nagare runs have no back-reference to the AIPM Task that triggered them.
The link is one-way: `Task.nagare_workflow_path` → YAML file in library.

### 6.2 Fix

**In AIPM:** When a Nagare run is created from a Task, store `tasks.nagare_run_id = run_id`.

**In Nagare:** Add optional metadata to run `state.json`:

```json
{
  "run_id": "...",
  "workflow_path": "...",
  "meta": {
    "task_id": "aipm-task-uuid",      ← new optional field
    "phase_id": "aipm-phase-uuid",    ← new optional field
    "project_id": "aipm-project-uuid" ← new optional field
  }
}
```

Implementation: `TaskState` gets a `set_origin_meta(task_id, phase_id, project_id)` method that writes to `state.json`.

This makes it possible to query "all Nagare runs for Phase X" by scanning run metadata.

---

## 7. Artefacts / Warehouse (no DB change needed)

Artefacts belong to a Phase via the filesystem, not by FK. The Phase record stores `folder_path` (real filesystem path, e.g. `/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/Reading/`).

KASUMI objects and temporary artefacts are written into that folder by the agent.
No database linkage by ID is required — the folder hierarchy IS the containment.

**Rule:** When creating a new Phase, the system should:
1. Accept a `folder_path` from the user (or default to `{project.folder_path}/{phase.name}/`)
2. Create the folder if it doesn't exist
3. Store the resolved absolute path in `phases.folder_path`

---

## 8. Frontend Changes (Minato / Shimanto panel)

### 8.1 Shimanto panel — Projects tab

Current: calls `/api/shimanto/projects`, gets a mix of Projects + Phases
Fix: call `/api/shimanto/projects` for true projects only, then load phases separately via `/api/projects/{id}/phases`

### 8.2 Shimanto panel — PHASES list

Current: PHASES list is populated from the same `/api/shimanto/projects` call
Fix: PHASES list calls `/api/shimanto/phases?project_id={active_project_id}`

### 8.3 Task creation modal

Add required field: Phase selector (dropdown of phases under active project)
Keep optional: Nagare Workflow picker (points to library YAML)

### 8.4 Milestone Gantt (future — Observability menu)

When Phase `timeline_mode = "calendar"`, Observability menu can render a Gantt chart of:
- Phases (horizontal bars, planned vs actual dates)
- Milestones (diamond markers within each Phase)
- Task completion % as fill color

This is a separate future feature — data model is ready after this refactor.

---

## 9. Implementation Order

| Step | What | Files affected | Risk |
|---|---|---|---|
| 1 | Add `PhaseStatus` enum | `domain/enums.py` | Low |
| 2 | Add `Phase` model, update `Project`/`Milestone`/`Task` models | `persistence/models.py` | Medium |
| 3 | Write migration script (create phases table, migrate data) | `migrations/001_add_phases_table.py` | Medium |
| 4 | Add Phase CRUD endpoints | `main.py` (AIPM) | Low |
| 5 | Update `/api/shimanto/projects` to return true projects only | `main.py` (AIPM) | Low |
| 6 | Add `/api/shimanto/phases` endpoint | `main.py` (AIPM) | Low |
| 7 | Update Shimanto adapter in Minato backend | `minato/plugins/shimanto/aipm_adapter.py` | Medium |
| 8 | Update frontend — projects vs phases data sources | Minato frontend `useProjectStore`, `useShimantoGanttData` | Medium |
| 9 | Add Nagare run `meta.task_id` to `TaskState` | `nagare/engine/state.py` | Low |
| 10 | Update AIPM: store `nagare_run_id` on Task when run is triggered | AIPM task trigger logic | Low |
| 11 | Run data migration on live DB | `aipm.db` | **HIGH — backup first** |

---

## 10. What Does NOT Change

- Nagare workflow YAML files — library stays as-is, reusable across phases ✅
- Nagare run filesystem structure (`/flow/runs/{run_id}/`) — unchanged ✅
- Artefact storage — filesystem under phase folder, no DB ID linkage ✅
- Milestone table structure (except `project_id` → `phase_id`) ✅
- Excel export/import round-trip — will need sheet updates for Phase tab ⚠️

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Live DB migration breaks existing data | Backup `aipm.db` before any migration; use soft-delete not hard delete |
| Shimanto adapter breaks during transition | Feature-flag the new phase endpoints; keep old endpoints alive until frontend migrated |
| `milestone_id` on Task was NOT nullable in SQLAlchemy | Migration must set all existing `milestone_id` to NULL-safe before making `phase_id` required |
| Duplicate project records (BARRY_PHD vs BARRY_S_PHD) | Merge tasks/phases under canonical project before deleting duplicate |
