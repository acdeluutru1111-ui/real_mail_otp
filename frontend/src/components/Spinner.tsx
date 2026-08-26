export function Spinner({ label }: { label?: string }) {
  return (
    <div className="spinner" role="status" aria-live="polite">
      <span className="spinner-dot" />
      <span>{label ?? 'Loading…'}</span>
    </div>
  );
}

export default Spinner;
