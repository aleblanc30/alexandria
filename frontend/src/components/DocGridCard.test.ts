import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { DocumentListItem } from '@/api/client'
import DocGridCard from './DocGridCard.vue'

// First component test in the suite. @vue/test-utils was
// already a dependency and unused; this is the pattern for the rest.

function doc(overrides: Partial<DocumentListItem> = {}): DocumentListItem {
  return {
    id: 1,
    source: 'zotero',
    source_id: 'ABC123',
    title: 'Attention Is All You Need',
    description: 'The transformer paper.',
    url_or_path: 'https://arxiv.org/abs/1706.03762',
    archive_url: null,
    zotero_attachment_key: null,
    source_tags: ['nlp'],
    cluster_l1_tags: ['machine learning'],
    cluster_l2_tags: [],
    learned_tags: [],
    ...overrides,
  }
}

describe('DocGridCard', () => {
  it('renders the title and description', () => {
    const w = mount(DocGridCard, { props: { doc: doc() } })

    expect(w.find('.grid-card-title').text()).toBe('Attention Is All You Need')
    expect(w.find('.grid-card-desc').text()).toBe('The transformer paper.')
  })

  it('falls back to Untitled when the title is empty', () => {
    const w = mount(DocGridCard, { props: { doc: doc({ title: '' }) } })

    expect(w.find('.grid-card-title').text()).toBe('Untitled')
  })

  it('omits the description block when there is none', () => {
    const w = mount(DocGridCard, { props: { doc: doc({ description: '' }) } })

    expect(w.find('.grid-card-desc').exists()).toBe(false)
  })

  it('renders source and cluster tags with their own pill classes', () => {
    const w = mount(DocGridCard, {
      props: { doc: doc({ source_tags: ['nlp'], cluster_l2_tags: ['transformers'] }) },
    })

    expect(w.find('.tag-pill--source').text()).toBe('#nlp')
    expect(w.find('.tag-pill--cluster_l1').text()).toBe('machine learning')
    expect(w.find('.tag-pill--cluster_l2').text()).toBe('transformers')
  })

  it('renders learned tags with the learned pill class', () => {
    const w = mount(DocGridCard, {
      props: { doc: doc({ source_tags: [], cluster_l1_tags: [], learned_tags: ['to-read'] }) },
    })

    expect(w.find('.tag-pill--learned').text()).toBe('to-read')
  })

  it('omits the tag row when the document has no tags at all', () => {
    const w = mount(DocGridCard, {
      props: {
        doc: doc({ source_tags: [], cluster_l1_tags: [], cluster_l2_tags: [], learned_tags: [] }),
      },
    })

    expect(w.find('.grid-card-tags').exists()).toBe(false)
  })

  it('emits click when the card is clicked', async () => {
    const w = mount(DocGridCard, { props: { doc: doc() } })

    await w.find('.grid-card').trigger('click')

    expect(w.emitted('click')).toHaveLength(1)
  })

  it('shows no checkbox outside pick mode', () => {
    const w = mount(DocGridCard, { props: { doc: doc() } })

    expect(w.find('.grid-card-check').exists()).toBe(false)
  })

  it('emits toggle-check from the checkbox in pick mode', async () => {
    const w = mount(DocGridCard, { props: { doc: doc(), pickMode: true, checked: false } })

    await w.find('.grid-card-check input').trigger('change')

    expect(w.emitted('toggle-check')).toHaveLength(1)
  })

  it('offers an open-in-new-tab button for a document with a URL', () => {
    const w = mount(DocGridCard, { props: { doc: doc() } })

    expect(w.find('.grid-card-link').exists()).toBe(true)
  })

  it('offers no link for a document with neither URL nor archive copy', () => {
    const w = mount(DocGridCard, {
      props: { doc: doc({ url_or_path: null, archive_url: null }) },
    })

    expect(w.find('.grid-card-link').exists()).toBe(false)
  })
})
