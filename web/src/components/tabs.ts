import { ChartLine, Columns2, MessageSquare, Users, Workflow } from "lucide-vue-next";
import type { Tab } from "@/router";

export const TABS = [
  { tab: "chat", label: "Chat", to: "/", icon: MessageSquare },
  { tab: "board", label: "Board", to: "/board", icon: Columns2 },
  { tab: "workflows", label: "Workflows", to: "/workflows", icon: Workflow },
  { tab: "metrics", label: "Metrics", to: "/metrics", icon: ChartLine },
  { tab: "admin", label: "Admin", to: "/admin", icon: Users },
] as const satisfies readonly { tab: Tab; label: string; to: string; icon: unknown }[];
