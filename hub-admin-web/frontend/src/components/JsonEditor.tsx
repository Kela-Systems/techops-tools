import CodeMirror from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";

export default function JsonEditor({
  value,
  onChange,
  height = "320px",
}: {
  value: string;
  onChange: (value: string) => void;
  height?: string;
}) {
  return (
    <div className="json-editor">
      <CodeMirror
        value={value}
        onChange={onChange}
        height={height}
        theme="dark"
        extensions={[json()]}
        basicSetup={{ foldGutter: true, lineNumbers: true }}
      />
    </div>
  );
}

export function parseJsonObject(
  text: string,
): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  try {
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return { ok: false, error: "expected a JSON object" };
    }
    return { ok: true, value };
  } catch (e) {
    return { ok: false, error: String((e as Error).message) };
  }
}
