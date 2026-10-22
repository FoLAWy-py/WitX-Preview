import { useEffect, useState, type ComponentType } from "react";

type Formatter = ComponentType<{ text: string }>;

let formatter: Formatter | undefined;
let loading: Promise<void> | undefined;

/** Loads the Markdown formatter once; text shown after that is formatted at once. */
export function loadMarkdown(): Promise<void> {
  loading ??= import("./Markdown").then(
    (module) => {
      formatter = module.default;
    },
    (error: unknown) => {
      loading = undefined;
      throw error;
    },
  );
  return loading;
}

/** Agent text, formatted once the formatter has loaded; plain until then, or if it cannot load. */
export function Rich({ text }: { text: string }) {
  const [Markdown, setMarkdown] = useState(() => formatter);
  useEffect(() => {
    if (Markdown) return;
    let live = true;
    loadMarkdown().then(
      () => live && setMarkdown(() => formatter),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [Markdown]);
  if (Markdown) return <Markdown text={text} />;
  return <p className="whitespace-pre-wrap break-words">{text}</p>;
}
