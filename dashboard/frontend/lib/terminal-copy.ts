"use client";

import { useI18n, type Locale } from "./i18n";

export const TERMINAL_COPY = {
  en: {
    command: "Command Center",
    live: "Live Portfolio",
    strategy: "Strategy",
    execution: "Execution",
    risk: "Risk",
    performance: "Performance",
    capital: "Capital",
    compare: "Paper vs Live",
    research: "Research",
    system: "System",
    readOnly: "READ-ONLY TERMINAL",
    realMoney: "REAL MONEY",
    unavailable: "Not available from the authoritative source",
  },
  ko: {
    command: "커맨드 센터",
    live: "실계좌 포트폴리오",
    strategy: "전략",
    execution: "체결",
    risk: "리스크",
    performance: "성과",
    capital: "자본",
    compare: "페이퍼 vs 실계좌",
    research: "리서치",
    system: "시스템",
    readOnly: "읽기 전용 터미널",
    realMoney: "실제 자금",
    unavailable: "권위 있는 데이터 소스에서 제공되지 않음",
  },
} as const;

export type TerminalCopyKey = keyof (typeof TERMINAL_COPY)["en"];

export function useTerminalCopy(): (key: TerminalCopyKey) => string {
  const { locale } = useI18n();
  const catalogue = TERMINAL_COPY[locale as Locale] ?? TERMINAL_COPY.en;
  return (key) => catalogue[key];
}
