import {
  ArrowRight,
  Building2,
  CalendarDays,
  Phone,
  Plus,
  Users,
} from "../components/icons";
import { Link } from "react-router-dom";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { Async, Badge, Empty, PageHeader } from "../components/ui";
import { date, label, list } from "../utils/format";
function callTime(seconds?: number) {
  const minutes = Math.round((seconds || 0) / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function Dashboard() {
  const query = useApi("/dashboard");
  const { session } = useAuth();
  const d = query.data || {},
    m = d.metrics || {};
  const cards = [
    ["Total leads", "totalLeads", Users, "Your active customer pipeline"],
    [
      "Qualified leads",
      "qualifiedLeads",
      Users,
      "Ready for the next conversation",
    ],
    [
      "Available units",
      "availableUnits",
      Building2,
      "Properties ready to recommend",
    ],
    [
      "Site visits booked",
      "siteVisitsBooked",
      CalendarDays,
      "Appointments across your workspace",
    ],
    [
      "Total call time",
      "totalCallSeconds",
      Phone,
      `Across ${m.callsHandled ?? 0} calls handled`,
    ],
  ];
  return (
    <>
      <PageHeader
        title={`Good to see you, ${session?.user.name?.split(" ")[0] || "there"}.`}
        description="Here’s what’s happening across your real estate workspace."
        action={
          <Link className="btn-primary" to="/leads?create=true">
            <Plus size={17} />
            Add lead
          </Link>
        }
      />
      <Async query={query}>
        {d.demo && (
          <div className="demo-strip">
            <span className="demo-dot" />
            <strong>Demo workspace</strong>
            <span>
              You’re viewing seeded sample records. External services may use
              simulated responses.
            </span>
          </div>
        )}
        <div className="metric-grid">
          {cards.map(([title, key, Icon, help]) => (
            <div className="metric-card" key={String(key)}>
              <div className="flex justify-between items-center">
                <span>{String(title)}</span>
                <span className="metric-icon">
                  <Icon size={18} />
                </span>
              </div>
              <strong>
                {key === "totalCallSeconds"
                  ? callTime(m.totalCallSeconds)
                  : (m[String(key)] ?? 0)}
              </strong>
              <small>{String(help)}</small>
            </div>
          ))}
        </div>
        <div className="dashboard-grid">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">YOUR PIPELINE</span>
                <h2>From interest to a visit</h2>
              </div>
              <Link to="/leads" className="text-link">
                View leads <ArrowRight size={15} />
              </Link>
            </div>
            <div className="pipeline">
              {[
                ["New", "newLeads"],
                ["Qualified", "qualifiedLeads"],
                ["Visits booked", "siteVisitsBooked"],
              ].map(([name, key], i) => (
                <div key={key}>
                  <span className="pipeline-number">0{i + 1}</span>
                  <h3>{m[key] ?? 0}</h3>
                  <p>{name}</p>
                  <div className="pipeline-bar">
                    <span
                      style={{
                        width: `${Math.max(2, Math.min(100, ((m[key] || 0) / Math.max(1, m.totalLeads || 0)) * 100))}%`,
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
            <div className="pipeline-footer">
              <span>
                <Building2 size={16} />
                {m.totalProjects ?? 0} projects
              </span>
              <span>
                <Phone size={16} />
                {m.callsHandled ?? 0} calls handled
              </span>
              <span>
                <CalendarDays size={16} />
                {m.upcomingSiteVisits ?? list(d.upcomingVisits).length} upcoming
                visits
              </span>
            </div>
          </section>
          <section className="next-step-card">
            <span className="eyebrow">MAKE THE RIGHT CONNECTION</span>
            <h2>
              A property that fits.
              <br />A customer who feels heard.
            </h2>
            <p>
              Match budget, location, and preferences to available inventory.
            </p>
            <Link to="/recommendations" className="btn-secondary">
              Find matching properties <ArrowRight size={17} />
            </Link>
          </section>
        </div>
        <div className="dashboard-grid">
          <section className="panel">
            <div className="panel-heading">
              <h2>Upcoming site visits</h2>
              <Link to="/site-visits" className="text-link">
                View schedule <ArrowRight size={15} />
              </Link>
            </div>
            {list(d.upcomingVisits).length ? (
              list(d.upcomingVisits)
                .slice(0, 5)
                .map((v: any) => (
                  <Link
                    className="schedule-row"
                    to={`/site-visits/${v.id}`}
                    key={v.id}
                  >
                    <div className="calendar-tile">
                      <CalendarDays size={20} />
                    </div>
                    <div className="flex-1">
                      <strong>
                        {v.leadName || v.projectName || "Site visit"}
                      </strong>
                      <small>{date(v.scheduledAt)}</small>
                    </div>
                    <Badge value={v.status} />
                  </Link>
                ))
            ) : (
              <Empty
                title="A little room in the calendar"
                description="Book a visit when a lead is ready to see a property."
              />
            )}
          </section>
          <section className="panel">
            <div className="panel-heading">
              <h2>Recent activity</h2>
              <span className="text-muted text-xs">Workspace updates</span>
            </div>
            {list(d.recentActivities).length ? (
              <div className="activity-list">
                {list(d.recentActivities)
                  .slice(0, 6)
                  .map((a: any, i: number) => (
                    <div className="activity-row" key={a.id || i}>
                      <span className="activity-dot" />
                      <div>
                        <strong>
                          {a.description || label(a.action || a.type)}
                        </strong>
                        <small>{date(a.createdAt || a.timestamp)}</small>
                      </div>
                    </div>
                  ))}
              </div>
            ) : (
              <Empty
                title="Your workspace is ready"
                description="Project, lead and appointment updates will appear here."
              />
            )}
          </section>
        </div>
        <section className="panel mt-6">
          <div className="panel-heading">
            <h2>Upcoming follow-ups</h2>
            <Link to="/leads" className="text-link">
              Manage pipeline <ArrowRight size={15} />
            </Link>
          </div>
          {list(d.followUps).length ? (
            list(d.followUps).map((l: any) => (
              <Link key={l.id} className="schedule-row" to={`/leads/${l.id}`}>
                <span className="avatar">{l.name?.slice(0, 2)}</span>
                <strong className="flex-1">{l.name}</strong>
                <span className="text-muted">{date(l.followUpAt)}</span>
                <Badge value={l.status} />
              </Link>
            ))
          ) : (
            <p className="p-6 text-muted">
              No scheduled follow-ups. Add a follow-up date to a lead to keep
              the conversation moving.
            </p>
          )}
        </section>
      </Async>
    </>
  );
}
