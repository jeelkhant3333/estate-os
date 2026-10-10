import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, Link, useLocation } from "react-router-dom";
import {
  ArrowUpRight,
  Bell,
  Building2,
  CalendarDays,
  ChevronRight,
  Handshake,
  LayoutDashboard,
  LogOut,
  Menu,
  Phone,
  Settings,
  Sparkles,
  Users,
  FolderOpen,
  X,
} from "../components/icons";
import { useAuth } from "../hooks/useAuth";
import { CustomSelect } from "../components/CustomSelect";
const navigation = [
  { to: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { to: "/projects", label: "Projects & inventory", icon: Building2 },
  { to: "/leads", label: "Leads", icon: Users },
  { to: "/calls", label: "Conversations", icon: Phone },
  { to: "/recommendations", label: "Recommendations", icon: Sparkles },
  { to: "/site-visits", label: "Site visits", icon: CalendarDays },
  { to: "/handovers", label: "Handovers", icon: Handshake },
  { to: "/files", label: "File management", icon: FolderOpen },
];
export function AppLayout() {
  const { session, workspaceId, switchWorkspace, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    closeButton.current?.focus();
    function onEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        menuButton.current?.focus();
      }
    }
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [open]);
  function closeNavigation() {
    setOpen(false);
    if (open) menuButton.current?.focus();
  }
  const location = useLocation();
  const current =
    navigation.find((n) => location.pathname.startsWith(n.to))?.label ||
    "Workspace";
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {open && (
        <button
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={closeNavigation}
        />
      )}
      <aside
        id="workspace-sidebar"
        className={`sidebar ${open ? "is-open" : ""}`}
      >
        <div className="flex items-center">
          <Link to="/dashboard" className="brand">
            <span className="brand-icon">
              <Building2 size={23} />
            </span>
            <span>
              Estra<span className="font-normal">OS</span>
            </span>
          </Link>
          <button
            ref={closeButton}
            className="mobile-close icon-btn"
            aria-label="Close navigation"
            onClick={(e) => {
              e.preventDefault();
              closeNavigation();
            }}
          >
            <X size={19} />
          </button>
        </div>
        <div className="nav-caption">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {navigation.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              onClick={closeNavigation}
              className={({ isActive }) =>
                `nav-item text-sm ${isActive ? "active" : ""}`
              }
            >
              <n.icon size={19} />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="handoff-note">
            <div className="flex items-center gap-2 font-semibold">
              <Handshake size={17} /> Every lead, a next step.
            </div>
            <p>From first conversation to a confident human handover.</p>
            <Link to="/handovers">
              View handovers <ArrowUpRight size={14} />
            </Link>
          </div>
          <NavLink
            to="/settings/profile"
            className="nav-item"
            onClick={closeNavigation}
          >
            <Settings size={18} />
            Profile & access
          </NavLink>
          <button
            className="user-block"
            onClick={() => void logout()}
            title="Sign out"
          >
            <span className="avatar">
              {session?.user.name?.slice(0, 2).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1 text-left">
              <strong>{session?.user.name}</strong>
              <small>Sign out of your workspace</small>
            </span>
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="flex items-center gap-3">
            <button
              ref={menuButton}
              className="icon-btn mobile-menu"
              aria-label="Open navigation"
              aria-expanded={open}
              aria-controls="workspace-sidebar"
              onClick={() => setOpen(true)}
            >
              <Menu size={21} />
            </button>
            <span className="text-muted text-base hidden sm:inline">Workspace</span>
            <ChevronRight size={14} className="text-muted hidden sm:inline" />
            <strong className="text-base">{current}</strong>
          </div>
          <div className="flex items-center gap-4">
            <div className="workspace-switcher">
              <CustomSelect
                id="workspace"
                label="Connected workspace"
                value={workspaceId}
                options={(session?.workspaces || []).map((w) => ({ value: w.id, label: w.name }))}
                onChange={switchWorkspace}
              />
            </div>
            <Link
              to="/notifications"
              aria-label="Notifications"
              className="icon-btn"
            >
              <Bell size={19} />
            </Link>
            <Link
              to="/settings/profile"
              className="avatar avatar-small"
              aria-label="Your profile"
            >
              {session?.user.name?.slice(0, 2).toUpperCase()}
            </Link>
          </div>
        </header>
        <main id="main" className="main-content" key={workspaceId}>
          <Outlet />
        </main>
        <footer>
          <span>© {new Date().getFullYear()} EstraOS</span> <span>Built around better customer journeys.</span>
        </footer>
      </div>
    </div>
  );
}
