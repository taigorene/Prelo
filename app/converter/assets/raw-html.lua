--[[
raw-html.lua - converts raw HTML embedded in Markdown into native pandoc elements.

Applied while parsing Markdown for the DOCX output: the DOCX writer silently drops
raw HTML, so tags such as <br>, <sup>, <kbd>, <img>, <a>, <details> or HTML tables
would otherwise disappear together with their meaning.

* Block HTML is re-read with pandoc's HTML reader (comments are removed and a
  <summary> becomes a bold paragraph); <pre> becomes a code block.
* Alignment (align="center"/"right", text-align, <center>) of a block is kept: its
  paragraphs go into a Div with the "Centered"/"Right Aligned" custom style and its
  headings get an "align" attribute (applied after pandoc, see docx.py). An opening
  <div>/<p> and its closing tag in separate blocks (Markdown in between) are paired.
* Inline HTML arrives as separate tokens (RawInline "<sup>", Str "2", RawInline
  "</sup>"); paired tags within the same inline list are turned into the matching
  element, void tags (<br>, <img>, <input type=checkbox>) are converted, and any
  other tag is dropped while its content is kept.

Everything here runs in linear time on hostile input (no backtracking patterns on
whole blocks, bounded nesting).
]]

local HTML_FORMATS = { html = true, html4 = true, html5 = true }
-- Inline elements nested deeper than this lose their markup (content is kept).
local MAX_INLINE_DEPTH = 64
local ALIGN_STYLES = { center = 'Centered', right = 'Right Aligned' }
-- Block elements whose opening and closing tags may sit in different blocks.
local SPLIT_CONTAINERS = { div = true, p = true, center = true }

local function is_html(el)
  return HTML_FORMATS[el.format:lower()] == true
end

-- Removes <!-- comments --> (an unterminated comment runs to the end).
local function strip_comments(text)
  if not text:find('<!--', 1, true) then
    return text
  end
  local parts = {}
  local position = 1
  while true do
    local start = text:find('<!--', position, true)
    if not start then
      parts[#parts + 1] = text:sub(position)
      break
    end
    parts[#parts + 1] = text:sub(position, start - 1)
    local finish = text:find('-->', start + 4, true)
    if not finish then
      break
    end
    position = finish + 3
  end
  return table.concat(parts)
end

-- <summary>x</summary> -> <p><strong>x</strong></p>
local function bold_summaries(text)
  local lower = text:lower()
  if not lower:find('<summary', 1, true) then
    return text
  end
  local parts = {}
  local position = 1
  while true do
    local open_start = lower:find('<summary', position, true)
    if not open_start then
      break
    end
    local open_end = lower:find('>', open_start + 8, true)
    if not open_end then
      break
    end
    if not lower:sub(open_start + 8, open_start + 8):match('[%s>/]') then
      parts[#parts + 1] = text:sub(position, open_end) -- another tag (<summaryx>)
      position = open_end + 1
    else
      local close_start = lower:find('</summary', open_end + 1, true)
      local close_end = close_start and lower:find('>', close_start + 9, true)
      if not close_end then
        break
      end
      parts[#parts + 1] = text:sub(position, open_start - 1)
      parts[#parts + 1] = '<p><strong>' .. text:sub(open_end + 1, close_start - 1)
        .. '</strong></p>'
      position = close_end + 1
    end
  end
  parts[#parts + 1] = text:sub(position)
  return table.concat(parts)
end

local function read_html(text)
  local ok, doc = pcall(pandoc.read, text, 'html')
  if ok then
    return doc.blocks
  end
  return nil
end

-- First inline element of type `tag` found when reading `html`.
local function first_inline(html, tag)
  local blocks = read_html(html)
  if not blocks then
    return nil
  end
  local found = nil
  blocks:walk({
    [tag] = function(el)
      found = found or el
    end,
  })
  return found
end

local function to_code(content)
  return pandoc.Code(pandoc.utils.stringify(content))
end

local WRAPPERS = {
  sup = pandoc.Superscript,
  sub = pandoc.Subscript,
  b = pandoc.Strong,
  strong = pandoc.Strong,
  i = pandoc.Emph,
  em = pandoc.Emph,
  cite = pandoc.Emph,
  dfn = pandoc.Emph,
  var = pandoc.Emph,
  u = pandoc.Underline,
  ins = pandoc.Underline,
  s = pandoc.Strikeout,
  del = pandoc.Strikeout,
  strike = pandoc.Strikeout,
  kbd = to_code,
  code = to_code,
  samp = to_code,
  tt = to_code,
  mark = function(content)
    return pandoc.Span(content, pandoc.Attr('', { 'mark' }))
  end,
  q = function(content)
    return pandoc.Quoted('DoubleQuote', content)
  end,
}

local VOID = {
  area = true, base = true, br = true, col = true, embed = true, hr = true, img = true,
  input = true, link = true, meta = true, param = true, source = true, track = true,
  wbr = true,
}

-- Returns name (lower case), closing?, self-closing?, attribute text.
local function parse_tag(text)
  local closing, name, rest = text:match('^%s*<(/?)(%a[%w:%-]*)(.-)>%s*$')
  if not name then
    return nil
  end
  return name:lower(), closing == '/', rest:match('/%s*$') ~= nil, rest
end

local function anchor_id(attributes)
  return attributes:match('%sid%s*=%s*["\']?([^"\'%s>]+)')
    or attributes:match('%sname%s*=%s*["\']?([^"\'%s>]+)')
end

local function make_link(open_tag, attributes, content)
  local link = first_inline(open_tag .. 'x</a>', 'Link')
  if link then
    return pandoc.Link(content, link.target, link.title, link.attr)
  end
  return pandoc.Span(content, pandoc.Attr(anchor_id(attributes) or ''))
end

local function make_span(open_tag, content)
  local span = first_inline(open_tag .. 'x</span>', 'Span')
  return pandoc.Span(content, span and span.attr or pandoc.Attr())
end

local function checkbox(attributes)
  local lower = attributes:lower()
  if not lower:match('type%s*=%s*["\']?checkbox') then
    return nil
  end
  local checked = lower:match('%f[%w]checked%f[^%w]') ~= nil
  return pandoc.Str(checked and '☒' or '☐')
end

-- Element that replaces a closed tag pair (a list of inlines).
local function close_element(frame)
  local wrap = WRAPPERS[frame.name]
  if wrap then
    return { wrap(frame.content) }
  elseif frame.name == 'a' then
    return { make_link(frame.open, frame.attributes, frame.content) }
  elseif frame.name == 'span' then
    return { make_span(frame.open, frame.content) }
  end
  return frame.content
end

local function void_element(name, text, attributes)
  if name == 'br' then
    return pandoc.LineBreak()
  elseif name == 'img' then
    return first_inline(text, 'Image')
  elseif name == 'input' then
    return checkbox(attributes)
  elseif name == 'a' then -- <a name="x"/>
    local id = anchor_id(attributes)
    return id and pandoc.Span({}, pandoc.Attr(id)) or nil
  end
  return nil
end

local function convert_inlines(inlines)
  local stack = { { content = pandoc.Inlines({}) } }
  local open = {} -- number of open frames per tag name
  local after_break = false -- just converted a <br>: drop the line's trailing break
  local function top()
    return stack[#stack]
  end
  local function pop()
    local frame = table.remove(stack)
    open[frame.name] = open[frame.name] - 1
    return frame
  end

  for _, el in ipairs(inlines) do
    local skip = after_break and (el.t == 'SoftBreak' or el.t == 'Space')
    after_break = false
    if skip then
      -- A <br> at the end of a source line: the line break would add a leading space.
    elseif el.t == 'RawInline' and is_html(el) then
      local name, closing, self_closing, attributes = parse_tag(el.text)
      if not name then
        -- Comments, doctypes and fragments that are not a single tag are dropped.
      elseif closing then
        if (open[name] or 0) > 0 then
          -- Tags left open inside the closed element lose their markup, keep their text.
          while top().name ~= name do
            local frame = pop()
            top().content:extend(frame.content)
          end
          local frame = pop()
          top().content:extend(close_element(frame))
        end
      elseif VOID[name] or self_closing then
        local replacement = void_element(name, el.text, attributes)
        if replacement then
          if replacement.t == 'LineBreak' then
            local content = top().content
            local last = content[#content]
            if last and (last.t == 'Space' or last.t == 'SoftBreak') then
              content:remove(#content)
            end
            after_break = true
          end
          top().content:insert(replacement)
        end
      elseif #stack <= MAX_INLINE_DEPTH then
        table.insert(stack, {
          name = name,
          open = el.text,
          attributes = attributes,
          content = pandoc.Inlines({}),
        })
        open[name] = (open[name] or 0) + 1
      end
    else
      top().content:insert(el)
    end
  end

  while #stack > 1 do
    local frame = table.remove(stack)
    top().content:extend(frame.content)
  end
  return stack[1].content
end

function Inlines(inlines)
  for _, el in ipairs(inlines) do
    if el.t == 'RawInline' and is_html(el) then
      return convert_inlines(inlines)
    end
  end
  return nil
end

-- Text of HTML <pre> content read by pandoc (line breaks kept, no-break spaces as spaces).
local function preformatted_text(blocks)
  local out = {}
  local function inlines_text(inlines)
    for _, el in ipairs(inlines) do
      if el.t == 'Str' then
        out[#out + 1] = el.text
      elseif el.t == 'Space' then
        out[#out + 1] = ' '
      elseif el.t == 'SoftBreak' or el.t == 'LineBreak' then
        out[#out + 1] = '\n'
      elseif el.t == 'Code' then
        out[#out + 1] = el.text
      elseif el.content and el.t ~= 'Note' then
        inlines_text(el.content)
      end
    end
  end
  for index, block in ipairs(blocks) do
    if index > 1 then
      out[#out + 1] = '\n'
    end
    if block.t == 'Plain' or block.t == 'Para' then
      inlines_text(block.content)
    elseif block.t == 'CodeBlock' then
      out[#out + 1] = block.text
    end
  end
  local text = table.concat(out):gsub('\u{a0}', ' ')
  return (text:gsub('^\n', ''):gsub('\n+$', ''))
end

local function pre_block(text)
  local blocks = read_html(text)
  if not blocks then
    return nil
  end
  local lang = text:match('class%s*=%s*["\']?[^"\'>]-language%-([%w_+%-]+)')
    or text:match('class%s*=%s*["\']?[^"\'>]-lang%-([%w_+%-]+)')
  return pandoc.CodeBlock(preformatted_text(blocks), pandoc.Attr('', lang and { lang } or {}))
end

-- Alignment of the outer element of an HTML block: "center", "right" or nil.
local function outer_alignment(text)
  local name, attributes = text:match('^%s*<(%a%w*)([^>]*)>')
  if not name then
    return nil
  end
  if name:lower() == 'center' then
    return 'center'
  end
  attributes = ' ' .. attributes:lower()
  local align = attributes:match('[%s"\']align%s*=%s*["\']?(%a+)')
    or attributes:match('text%-align%s*:%s*(%a+)')
  if align == 'center' or align == 'right' then
    return align
  end
  return nil
end

local function aligned(blocks, align)
  if not align then
    return blocks
  end
  blocks = pandoc.Blocks(blocks):walk({
    Header = function(header)
      if not header.attributes.align then
        header.attributes.align = align
        return header
      end
    end,
  })
  return { pandoc.Div(blocks, pandoc.Attr('', {}, { { 'custom-style', ALIGN_STYLES[align] } })) }
end

-- Number of opening minus closing tags named `name` (lower case) in `text`.
local function balance(text, name)
  local lower = text:lower()
  local opens, closes = 0, 0
  for _ in lower:gmatch('<' .. name .. '[%s>/]') do
    opens = opens + 1
  end
  for _ in lower:gmatch('</' .. name .. '%s*>') do
    closes = closes + 1
  end
  return opens - closes
end

function RawBlock(el)
  if not is_html(el) then
    return nil
  end
  local text = strip_comments(el.text)
  if text:match('^%s*$') then
    return {}
  end
  local name = (text:match('^%s*<(%a%w*)') or ''):lower()
  if name == 'pre' and text:match('</[Pp][Rr][Ee]%s*>%s*$') then
    return pre_block(text) or {}
  end
  if SPLIT_CONTAINERS[name] and balance(text, name) > 0 then
    -- Opening tag whose closing tag comes in a later block: keep a marker for Blocks.
    local tag = text:match('^%s*(<[^>]*>)')
    local result = pandoc.Blocks({ pandoc.RawBlock('html', tag) })
    result:extend(read_html(bold_summaries(text:sub(#text:match('^%s*<[^>]*>') + 1))) or {})
    return result
  end
  local closer = (text:match('</(%a%w*)%s*>%s*$') or ''):lower()
  if SPLIT_CONTAINERS[closer] and balance(text, closer) < 0 then
    local before = text:sub(1, #text - #text:match('</%a%w*%s*>%s*$'))
    local result = pandoc.Blocks(read_html(bold_summaries(before)) or {})
    result:insert(pandoc.RawBlock('html', '</' .. closer .. '>'))
    return result
  end
  return aligned(read_html(bold_summaries(text)) or {}, outer_alignment(text))
end

-- Pairs the opening/closing markers left by RawBlock around the blocks between them.
function Blocks(blocks)
  local has_markers = false
  for _, el in ipairs(blocks) do
    if el.t == 'RawBlock' and is_html(el) then
      has_markers = true
      break
    end
  end
  if not has_markers then
    return nil
  end
  local result = pandoc.Blocks({})
  local stack = {}
  local function target()
    return #stack > 0 and stack[#stack].content or result
  end
  local function close(frame)
    target():extend(aligned(frame.content, frame.align))
  end
  for _, el in ipairs(blocks) do
    if el.t == 'RawBlock' and is_html(el) then
      local opening = el.text:match('^<(%a%w*)')
      local closing = el.text:match('^</(%a%w*)>$')
      if opening and SPLIT_CONTAINERS[opening:lower()] then
        stack[#stack + 1] = {
          name = opening:lower(),
          align = outer_alignment(el.text),
          content = pandoc.Blocks({}),
        }
      elseif closing then
        local index = nil
        for k = #stack, 1, -1 do
          if stack[k].name == closing:lower() then
            index = k
            break
          end
        end
        while index and #stack >= index do
          close(table.remove(stack))
        end
      end
      -- Other leftover raw HTML is dropped (the DOCX writer would drop it anyway).
    else
      target():insert(el)
    end
  end
  while #stack > 0 do
    close(table.remove(stack))
  end
  return result
end
