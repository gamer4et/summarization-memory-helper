import subprocess
import textwrap


def _run_summary_renderer_node(script: str, tmp_path):
    source = tmp_path / "summaryRenderer.mjs"
    source.write_text(
        __import__("pathlib").Path("frontend/js/summaryRenderer.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runner = tmp_path / "run.mjs"
    runner.write_text(script, encoding="utf-8")

    return subprocess.run(
        ["node", str(runner)],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )


def test_mermaid_repair_wraps_long_russian_node_labels(tmp_path):
    script = textwrap.dedent(
        r'''
        import assert from "node:assert/strict";
        import { repairMermaidDiagram } from "./summaryRenderer.mjs";

        const repaired = repairMermaidDiagram(`flowchart LR
        A[Упразднение и снижение функций внешнего менеджмента] -->|Приводит к| B[Снижение издержек]
        C[Создание и развитие новых каналов продаж] --> D[Рост ценности для покупателя]`);

        assert.match(repaired, /A\["`Упразднение и снижение\nфункций внешнего\nменеджмента`"\]/);
        assert.match(repaired, /C\["`Создание и развитие\nновых каналов продаж`"\]/);
        assert.match(repaired, /D\["`Рост ценности для\nпокупателя`"\]/);
        assert.match(repaired, /-->|"Приводит к"\| B\[Снижение издержек\]/);
        '''
    )

    _run_summary_renderer_node(script, tmp_path)


def test_mermaid_repair_preserves_existing_multiline_and_quoted_labels(tmp_path):
    script = textwrap.dedent(
        r'''
        import assert from "node:assert/strict";
        import { repairMermaidDiagram } from "./summaryRenderer.mjs";

        const source = `flowchart LR
        A["Already safe quoted label"] -->|already quoted| B["` + "`Already\nwrapped`" + `"]
        B --> C{{Очень длинная подпись ромба для проверки фигур Mermaid}}`;
        const repaired = repairMermaidDiagram(source);

        assert.match(repaired, /A\["Already safe quoted label"\]/);
        assert.match(repaired, /B\["`Already\nwrapped`"\]/);
        assert.match(repaired, /C\{\{"`Очень длинная подпись\nромба для проверки\nфигур Mermaid`"\}\}/);
        assert.match(repaired, /-->|"already quoted"\| B/);
        '''
    )

    _run_summary_renderer_node(script, tmp_path)
