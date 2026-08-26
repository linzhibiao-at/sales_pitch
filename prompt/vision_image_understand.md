分析用户上传的 FILA 相关服装图片。只输出 JSON：
{image_kind(single_item|full_outfit|scene|unknown), anchor_role(top|bottoms|shoes|dress|accessory|null), colors[], style_tags[], confidence}

- single_item：画面主体为单件服装平铺/挂拍/上身局部。
- full_outfit：整套穿搭参考图。
- scene：户外/门店场景且服装非单一主体。
- anchor_role：若 single_item，判断最可能品类；不确定填 null。
- confidence：0~1。
