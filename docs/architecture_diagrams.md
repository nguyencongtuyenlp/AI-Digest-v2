# Daily Digest Architecture Diagrams

File này giữ `Mermaid source of truth` cho 2 sơ đồ kiến trúc chính của dự án. Khi cần render lại asset ảnh, dùng `scripts/export_architecture_diagrams.sh`.

## 1. System Overview

<!-- diagram: system_overview -->
```mermaid
flowchart TB
  I["Inputs<br/>RSS, official blogs, GitHub signals,<br/>watchlist, DDG, HN, Reddit, Telegram channels,<br/>Facebook/social signals, Telegram feedback"]
  E["Entry points<br/>main.py, ui_server.py, scripts/launchd.plist"]
  O["Orchestration<br/>pipeline_runner.py:<br/>initial state, runtime preset, source health,<br/>process lock, model override"]
  G["Workflow engine<br/>digest/workflow/graph.py LangGraph StateGraph"]
  P["Processing<br/>gather -> normalize -> deduplicate -> collect_feedback<br/>-> early_rule_filter -> batch_classify_and_score<br/>-> batch_deep_process / batch_quick_compose<br/>-> merge_processed_articles -> delivery_judge<br/>-> save_notion -> summarize_vn -> quality_gate"]
  D["Review + delivery<br/>UI preview, main Telegram brief,<br/>run health, publish_ready"]
  S["Storage + artefacts<br/>SQLite, vector memory, Notion,<br/>reports, temporal snapshots, .runtime_archive/"]

  I --> E --> O --> G --> P
  P --> D
  P --> S
  S -. history / related context .-> P
  D -. feedback loop .-> I
```

## 2. Execution Flow

<!-- diagram: execution_flow -->
```mermaid
flowchart TB
  A["main.py / ui_server.py / scripts/launchd.plist"] --> B["pipeline_runner.run_pipeline()"]
  B --> C["build_initial_state()<br/>run_mode / run_profile / runtime_config"]
  C --> D["collect_source_health()<br/>notify_source_health_if_needed()"]
  D --> E["digest/workflow/graph.py invoke(initial_state)<br/>same graph for preview and publish"]

  subgraph Graph["Compiled LangGraph execution"]
    G1["gather"]
    G2["normalize_source"]
    G3["deduplicate"]
    G4["collect_feedback"]
    G5["early_rule_filter"]
    G6["batch_classify_and_score"]
    G7{"Fan-out by result"}
    G8["batch_deep_process<br/>chunked Send() for top_articles"]
    G9["batch_quick_compose<br/>for low_score_articles"]
    G10["merge_processed_articles"]
    G11["delivery_judge"]
    G12["save_notion"]
    G13["summarize_vn"]
    G14["quality_gate"]
    G15["send_telegram"]
    G16["generate_run_report"]
  end

  E --> G1 --> G2 --> G3 --> G4 --> G5 --> G6 --> G7
  G7 -->|top_articles| G8
  G7 -->|low_score_articles| G9
  G8 --> G10
  G9 --> G10
  G10 --> G11 --> G12 --> G13 --> G14 --> G15 --> G16
  G16 --> H["result + summary"]

  H --> I{"run_mode"}
  I -->|publish| J["Outputs already published during graph run"]
  I -->|preview| K["ui_server stores preview_state<br/>workspace_articles + telegram_messages"]
  K --> L["Approve Preview"]
  L --> M["publish_from_preview_state()"]
  M --> N["save_notion -> summarize_vn -> quality_gate<br/>-> send_telegram -> generate_run_report"]
```
