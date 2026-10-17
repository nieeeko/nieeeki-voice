"use client";

import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";

/** Was der Voice-Core per canvas_zeigen schickt (siehe voice-core/tools.py). */
export type CanvasItem =
  | { id?: string | null; kind: "tabelle"; title: string; columns: string[]; rows: string[][] }
  | { id?: string | null; kind: "code"; title: string; text: string; language?: string }
  | { id?: string | null; kind: "dokument"; title: string; text: string }
  | { id?: string | null; kind: "mermaid"; title: string; text: string };

const KIND_LABEL: Record<CanvasItem["kind"], string> = {
  tabelle: "Tabelle",
  code: "Code",
  dokument: "Dokument",
  mermaid: "Diagramm",
};

export function Canvas({ items }: { items: CanvasItem[] }) {
  const [picked, setPicked] = useState<number | null>(null);
  // Ohne eigene Auswahl zeigt das Canvas immer das Neueste.
  const index = picked !== null && picked < items.length ? picked : items.length - 1;
  const item = items[index];

  return (
    <div className="canvas-board">
      {items.length > 1 && (
        <nav className="canvas-tabs" aria-label="Frühere Inhalte">
          {items.map((it, i) => (
            <button
              key={`${i}-${it.title}`}
              className="canvas-tab"
              aria-current={i === index}
              onClick={() => setPicked(i === items.length - 1 ? null : i)}
            >
              {it.title}
            </button>
          ))}
        </nav>
      )}
      <article className="canvas-item">
        <header className="canvas-item-head">
          <span className="canvas-kind">{KIND_LABEL[item.kind]}</span>
          <h2>{item.title}</h2>
        </header>
        <CanvasBody item={item} />
      </article>
    </div>
  );
}

function CanvasBody({ item }: { item: CanvasItem }) {
  switch (item.kind) {
    case "tabelle":
      return (
        <div className="canvas-table-wrap">
          <table className="canvas-table">
            <thead>
              <tr>
                {item.columns.map((c, i) => (
                  <th key={i}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {item.rows.map((row, r) => (
                <tr key={r}>
                  {item.columns.map((_, c) => (
                    <td key={c}>{row[c] ?? ""}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "code":
      return <CodeView text={item.text} language={item.language} />;
    case "dokument":
      return <div className="canvas-doc">{renderMarkdown(item.text)}</div>;
    case "mermaid":
      return <MermaidView source={item.text} />;
  }
}

function CodeView({ text, language }: { text: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="canvas-code">
      <div className="canvas-code-bar">
        <span>{language || "text"}</span>
        <button
          onClick={async () => {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "Kopiert" : "Kopieren"}
        </button>
      </div>
      <pre>
        <code>{text}</code>
      </pre>
    </div>
  );
}

function MermaidView({ source }: { source: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        // strict: Mermaid saeubert das SVG selbst (kein HTML/Script aus dem Diagrammtext).
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "neutral" });
        const { svg } = await mermaid.render(`m${id}`, source);
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Diagramm konnte nicht gezeichnet werden.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [source, id]);

  return (
    <>
      <div className="canvas-mermaid" ref={ref} />
      {error && (
        <>
          <p className="error">Diagramm fehlerhaft: {error}</p>
          <CodeView text={source} language="mermaid" />
        </>
      )}
    </>
  );
}

/** Kleines, sicheres Markdown: #-Ueberschriften, Listen, Absaetze, **fett**, `code`. Kein HTML. */
function renderMarkdown(text: string): ReactNode {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const flushList = () => {
    if (list.length) {
      blocks.push(
        <ul key={`l${blocks.length}`}>
          {list.map((li, i) => (
            <li key={i}>{inline(li)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      list.push(bullet[1]);
      continue;
    }
    flushList();
    if (heading) {
      const level = heading[1].length;
      const Tag = (["h3", "h4", "h5"] as const)[level - 1];
      blocks.push(<Tag key={`h${blocks.length}`}>{inline(heading[2])}</Tag>);
    } else if (line.trim()) {
      blocks.push(<p key={`p${blocks.length}`}>{inline(line)}</p>);
    }
  }
  flushList();
  return blocks;
}

function inline(text: string): ReactNode {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}
