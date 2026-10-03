// Keep this contract aligned with services/titles.py; lengths are Unicode code points.
export const MAX_TITLE_LENGTH = 200;

export function titleError(value: string): string | null {
  if (!value.trim() || [...value].length > MAX_TITLE_LENGTH || /[\x00-\x1f\x7f]/.test(value)) {
    return `Supply a title of 1–${MAX_TITLE_LENGTH} characters without control characters.`;
  }
  return null;
}
