/** 记忆正文开头的 `[关于用户]`、`[对话摘要] 用户说：` 这类前缀是抽取器打的内部分类标记。
 *  以前原样摊在「moz 记得什么」里，读起来像日志而不像"她记住的事"。
 *  后端 memory_governance.normalized_content 洗的是判重用的形态，不是给人看的文案，所以这里单独一份。 */

const INTERNAL_TAG = /^\[([^\]]+)\]\s*/
const LEAD_IN = /^(AI回复要点|用户说|用户提到|用户表示)[：:]\s*/

/** 内部标记 → 一句人话的来路。关于用户不用标：那一整页说的都是他。 */
const TAG_CHIPS: Record<string, string> = {
  对话摘要: '那次聊天',
  用户情感状态: '当时的情绪',
  AI互动: '和她的相处',
  关于用户: '',
}
const LEAD_CHIPS: Record<string, string> = {
  AI回复要点: '她自己的话',
  用户说: '那次聊天里你说的',
  用户提到: '那次聊天里你说的',
  用户表示: '那次聊天里你说的',
}

/** 去掉内部分类标记和"用户说："这类转述开头，只留那句话本身。 */
export function stripInternal(content: string): string {
  return (content || '')
    .replace(INTERNAL_TAG, '')
    .replace(/^(?:AI回复要点|用户(?:说|提到|表示))[：:]\s*/, '')
}

/** text = 给用户看的那句话；chip = 这条是从哪来的（空字符串表示不用标） */
export function humanMemory(content: string): { text: string; chip: string } {
  const raw = content || ''
  const tag = (raw.match(INTERNAL_TAG) || ['', ''])[1]
  const lead = (raw.replace(INTERNAL_TAG, '').match(LEAD_IN) || ['', ''])[1]
  let text = stripInternal(raw)
  // [关于用户] 讲的就是正在看这一页的人，第三称的"用户"最不像人话
  if (tag === '关于用户') text = text.replace(/^用户/, '你')
  return { text, chip: LEAD_CHIPS[lead] || TAG_CHIPS[tag] || '' }
}
