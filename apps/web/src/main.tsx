import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// Before the first paint, so a dark page does not flash light (the provider takes over).
let theme: string | null = null;
try {
  theme = localStorage.getItem("witx.theme");
} catch {
  // no saved theme
}
if (
  theme === "dark" ||
  (theme !== "light" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches)
) {
  document.documentElement.classList.add("dark");
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
