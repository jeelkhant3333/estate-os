import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LeadDetail } from "../src/pages/Leads";
import { write } from "../src/api/client";

const lead = {
  id: "12",
  name: "Asha Patel",
  status: "QUALIFIED",
  source: "WEBSITE",
  recommendations: [{ id: "r1", projectName: "Aurum Heights", unitNumber: "A-301", reason: "Fits budget" }],
  calls: [{ id: "c1", createdAt: "2026-10-04T10:00:00Z", status: "COMPLETED", outcome: "Interested" }],
  siteVisits: [{ id: "v1", scheduledAt: "2026-10-05T10:00:00Z", status: "SCHEDULED" }],
  handovers: [{ id: "h1", createdAt: "2026-10-06T10:00:00Z", status: "PENDING" }],
  activities: [],
};
let activityFixtures: { id: string; description: string; createdAt: string }[] = [];
let callbackFixtures: { id: string; status: string; dueAt: string }[] = [];

beforeEach(() => { activityFixtures = []; callbackFixtures = []; vi.clearAllMocks(); });

vi.mock("../src/hooks/useApi", () => ({
  useApi: (path: string) => ({
    isPending: false,
    error: null,
    refetch: vi.fn(),
    data: path === "/members" ? [] : { ...lead, activities: activityFixtures, callbacks: callbackFixtures },
  }),
}));
vi.mock("../src/hooks/useAuth", () => ({ useAuth: () => ({ canManage: true }) }));
vi.mock("../src/api/client", () => ({ write: vi.fn().mockResolvedValue(undefined) }));
vi.mock("../src/services/query", () => ({ queryClient: { invalidateQueries: vi.fn() } }));

it("confirms lead removal and sends DELETE only after confirmation", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/leads/12"]}>
      <Routes>
        <Route path="/leads/:id" element={<LeadDetail />} />
        <Route path="/leads" element={<div>Leads list</div>} />
      </Routes>
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("button", { name: "Remove lead" }));
  expect(write).not.toHaveBeenCalledWith("/leads/12", expect.anything(), "DELETE");
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove lead" }));
  await user.click(screen.getByRole("button", { name: "Confirm removal" }));
  expect(write).toHaveBeenCalledWith("/leads/12", null, "DELETE");
  expect(await screen.findByText("Leads list")).toBeInTheDocument();
});

it("requires confirmation before cancelling a scheduled callback", async () => {
  callbackFixtures = [{ id: "cb1", status: "SCHEDULED", dueAt: "2026-10-07T10:00:00Z" }];
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/leads/12"]}>
      <Routes><Route path="/leads/:id" element={<LeadDetail />} /></Routes>
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(write).not.toHaveBeenCalled();
  expect(screen.getByRole("heading", { name: "Cancel callback?" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Confirm cancellation" }));
  expect(write).toHaveBeenCalledWith("/callbacks/cb1/cancel", {});
});

describe("lead detail related records", () => {
  it("starts with the five latest activities and reveals older ones in batches", async () => {
    activityFixtures = Array.from({ length: 12 }, (_, index) => ({
      id: `a${index + 1}`,
      description: `Timeline event ${index + 1}`,
      createdAt: new Date(Date.UTC(2026, 9, index + 1)).toISOString(),
    }));
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/leads/12"]}>
        <Routes>
          <Route path="/leads/:id" element={<LeadDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    const timeline = screen.getByRole("heading", { name: "Activity timeline" }).closest("section")!;
    const visibleEvents = () => within(timeline).getAllByText(/Timeline event \d+/).map((event) => event.textContent);
    expect(visibleEvents()).toEqual([
      "Timeline event 12", "Timeline event 11", "Timeline event 10", "Timeline event 9", "Timeline event 8",
    ]);

    await user.click(within(timeline).getByRole("button", { name: "Show 5 more" }));
    expect(visibleEvents()).toHaveLength(10);
    expect(within(timeline).queryByText("Timeline event 2")).not.toBeInTheDocument();

    await user.click(within(timeline).getByRole("button", { name: "Show 2 more" }));
    expect(visibleEvents()).toHaveLength(12);
    await user.click(within(timeline).getByRole("button", { name: "Show less" }));
    expect(visibleEvents()).toHaveLength(5);
  });

  it("does not offer more activities when there are five or fewer", () => {
    activityFixtures = Array.from({ length: 5 }, (_, index) => ({
      id: `a${index + 1}`,
      description: `Timeline event ${index + 1}`,
      createdAt: new Date(Date.UTC(2026, 9, index + 1)).toISOString(),
    }));
    render(
      <MemoryRouter initialEntries={["/leads/12"]}>
        <Routes>
          <Route path="/leads/:id" element={<LeadDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    const timeline = screen.getByRole("heading", { name: "Activity timeline" }).closest("section")!;
    expect(within(timeline).getAllByText(/Timeline event \d+/)).toHaveLength(5);
    expect(within(timeline).queryByRole("button", { name: /Show (more|less)/ })).not.toBeInTheDocument();
  });

  it("gives callbacks a full-width row with related records directly below", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/leads/12"]}>
        <Routes>
          <Route path="/leads/:id" element={<LeadDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    const columns = container.querySelector(".detail-columns");
    const [left, right, fullWidth] = Array.from(columns?.children ?? []);
    expect(left).toContainElement(screen.getByRole("heading", { name: "Conversation notes" }));
    expect(right).toContainElement(screen.getByRole("heading", { name: "Activity timeline" }));
    expect(fullWidth).toHaveClass("lead-detail-wide");
    expect(fullWidth).toContainElement(screen.getByRole("heading", { name: "Callbacks & scheduled calls" }));
    expect(fullWidth).toContainElement(screen.getByRole("heading", { name: "Related records" }));
    expect(fullWidth.querySelectorAll(":scope > section")[0]).toContainElement(screen.getByRole("heading", { name: "Callbacks & scheduled calls" }));
    expect(fullWidth.querySelectorAll(":scope > section")[1]).toContainElement(screen.getByRole("heading", { name: "Related records" }));
  });

  it("shows one related-record section at a time and lets people switch sections", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/leads/12"]}>
        <Routes>
          <Route path="/leads/:id" element={<LeadDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("tab", { name: /Suggested properties/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Aurum Heights")).toBeVisible();
    expect(screen.queryByText("Interested")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Call history/ }));
    expect(screen.getByRole("tab", { name: /Call history/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Interested")).toBeVisible();
    expect(screen.queryByText("Aurum Heights")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Site visits & handovers/ }));
    expect(screen.getByRole("link", { name: /5 Oct 2026/ })).toHaveAttribute("href", "/site-visits/v1");
    expect(screen.getByText(/Handover ·/)).toBeVisible();
    expect(screen.queryByText("Interested")).not.toBeInTheDocument();
  });

  it("supports keyboard switching between related-record tabs", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/leads/12"]}>
        <Routes>
          <Route path="/leads/:id" element={<LeadDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    screen.getByRole("tab", { name: /Suggested properties/ }).focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Call history/ })).toHaveFocus();
    expect(screen.getByText("Interested")).toBeVisible();
  });
});
