import { ReactNode } from "react";

interface FieldProps {
  label: string;
  children: ReactNode;
}

export function Field({ label, children }: FieldProps) {
  return (
    <label className="grid gap-1 text-sm font-medium text-zinc-700">
      {label}
      {children}
    </label>
  );
}
