import type { CategoriesSource, Category, ObjectKind } from '../types'
import { CATEGORIES } from '../generated/memory-contract'

export const CATEGORY_LABELS: Record<Category, string> = {
  personal_details: '个人信息', family: '家庭', professional_details: '职业',
  sports: '运动', travel: '出行', food: '饮食', music: '音乐', health: '健康',
  technology: '技术', hobbies: '兴趣', fashion: '时尚', entertainment: '娱乐',
  milestones: '里程碑', user_preferences: '偏好', misc: '其他',
}

export const CATEGORY_OPTIONS: [Category, string][] = CATEGORIES.map(category => [category, CATEGORY_LABELS[category]])
export const CATEGORY_SOURCE_LABELS: Record<CategoriesSource, string> = {
  unset: '未设置', llm: '模型判断', user: '人工设置',
}
export const OBJECT_KIND_LABELS: Record<ObjectKind, string> = {
  entity: '实体', literal: '字面值', unknown: '未知',
}

export function savedCategories(value: unknown): Category[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter((item): item is Category =>
    typeof item === 'string' && Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, item)))]
}
