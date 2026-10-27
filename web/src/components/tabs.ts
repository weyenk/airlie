import { BookOpen, ChartLine, Columns2, MessageSquare, Users, Workflow } from "lucide-vue-next";
import type { Tab } from "@/router";

export const TABS = [
  { tab: "chat", label: "Chat", to: "/", icon: MessageSquare },
  { tab: "workflows", label: "Workflows", to: "/workflows", icon: Workflow },
  { tab: "board", label: "Board", to: "/board", icon: Columns2 },
  { tab: "metrics", label: "Metrics", to: "/metrics", icon: ChartLine },
  { tab: "admin", label: "Admin", to: "/admin", icon: Users },
  { tab: "wiki", label: "Wiki", to: "/wiki", icon: BookOpen },
] as const satisfies readonly { tab: Tab; label: string; to: string; icon: unknown }[];
