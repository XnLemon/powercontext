import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

function App() {
  return (
    <main>
      <h1>PowerContext Evaluation Console</h1>
    </main>
  );
}

const root = document.getElementById("root");
if (root === null) throw new Error("Application root is missing.");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
