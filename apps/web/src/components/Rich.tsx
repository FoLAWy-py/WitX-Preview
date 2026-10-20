import { Suspense, lazy } from "react";

const Markdown = lazy(() => import("./Markdown"));

/** Agent text, formatted once the formatter has loaded; plain until then. */
export function Rich({ text }: { text: string }) {
  return (
    <Suspense
      fallback={<p className="whitespace-pre-wrap break-words">{text}</p>}
    >
      <Markdown text={text} />
    </Suspense>
  );
}
