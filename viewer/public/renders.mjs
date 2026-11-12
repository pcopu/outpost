export function selectRender(renders, requested) {
  return renders.find(file => file.sha256 === requested)
    || [...renders].reverse().find(file => file.reviewStatus !== 'rejected')
    || renders[0]
    || null;
}
