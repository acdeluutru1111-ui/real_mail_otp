// Pricing package catalog (mirrors dataForAgent pricing_packages).
// pay-as-you-go: 200đ = 1 read. Packages give bonus reads for a fixed top-up.

export const READ_PRICE_VND = 200;

export interface PackageInfo {
  code: 'pay-as-you-go' | 'starter' | 'popular' | 'pro';
  label: string;
  topupVnd: number | null; // null = any amount for pay-as-you-go
  reads: number | null; // null = derived from amount for pay-as-you-go
  perReadVnd: number;
}

export const PACKAGES: PackageInfo[] = [
  {
    code: 'pay-as-you-go',
    label: 'Pay as you go',
    topupVnd: null,
    reads: null,
    perReadVnd: 200,
  },
  {
    code: 'starter',
    label: 'Starter',
    topupVnd: 19000,
    reads: 150,
    perReadVnd: 127,
  },
  {
    code: 'popular',
    label: 'Popular',
    topupVnd: 29000,
    reads: 350,
    perReadVnd: 83,
  },
  { code: 'pro', label: 'Pro', topupVnd: 49000, reads: 800, perReadVnd: 61 },
];

export function formatVnd(amount: number): string {
  return `${amount.toLocaleString()}đ`;
}
