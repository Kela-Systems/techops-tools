import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { HubProvider, useHub } from "./hub";
import { ToastProvider } from "./toast";
import DeviceConfigsPage from "./pages/DeviceConfigsPage";
import IntegrationsPage from "./pages/IntegrationsPage";
import LinksPage from "./pages/LinksPage";
import ProfilesPage from "./pages/ProfilesPage";
import ServerPage from "./pages/ServerPage";

function ContextPicker() {
  const { contexts, context, setContext, contextsError } = useHub();
  if (contextsError) {
    return <span className="context-error" title={contextsError}>kubeconfig unavailable</span>;
  }
  return (
    <select
      className="context-picker"
      value={context ?? ""}
      onChange={(e) => setContext(e.target.value)}
      disabled={contexts.length === 0}
    >
      {contexts.length === 0 && <option value="">loading contexts…</option>}
      {contexts.map((c) => (
        <option key={c} value={c}>
          {c}
        </option>
      ))}
    </select>
  );
}

function Shell() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◉</span> hub-admin
        </div>
        <nav>
          <NavLink to="/integrations">Integrations</NavLink>
          <NavLink to="/links">Links</NavLink>
          <NavLink to="/profiles">Profiles</NavLink>
          <NavLink to="/device-configs">Device configs</NavLink>
          <NavLink to="/server">Server</NavLink>
        </nav>
        <div className="topbar-right">
          <span className="context-label">hub</span>
          <ContextPicker />
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/integrations" replace />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="/links" element={<LinksPage />} />
          <Route path="/profiles" element={<ProfilesPage />} />
          <Route path="/device-configs" element={<DeviceConfigsPage />} />
          <Route path="/server" element={<ServerPage />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <HubProvider>
        <Shell />
      </HubProvider>
    </ToastProvider>
  );
}
