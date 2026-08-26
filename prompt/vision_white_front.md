在缺省 cd/master 官方图时，用于从候选 URL 列表中判定「白底正面主图」优先级。只输出 JSON：
{chosen_url: string|null, rationale: string, confidence: number}

不得编造不在候选列表中的 URL。若无合适项，chosen_url 为 null。
